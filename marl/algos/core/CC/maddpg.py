from marl.algos.core.IL.ddpg import *
from marl.algos.utils.postprocessing import centralized_critic_q, CentralizedQValueMixin
from ray.rllib.agents.ddpg.ddpg_torch_policy import TargetNetworkMixin, ComputeTDErrorMixin
from ray.rllib.utils.torch_ops import convert_to_torch_tensor
from ray.rllib.utils.numpy import convert_to_numpy

torch, nn = try_import_torch()


def build_maddpg_models(policy, observation_space, action_space, config):
    num_outputs = int(np.product(observation_space.shape))

    policy_model_config = MODEL_DEFAULTS.copy()
    policy_model_config.update(config["policy_model"])
    q_model_config = MODEL_DEFAULTS.copy()
    q_model_config.update(config["Q_model"])

    policy.model = ModelCatalog.get_model_v2(
        obs_space=observation_space,
        action_space=action_space,
        num_outputs=num_outputs,
        model_config=config["model"],
        framework=config["framework"],
        default_model=MADDPG_RNN_TorchModel,
        name="rnnddpg_model",
        policy_model_config=policy_model_config,
        q_model_config=q_model_config,
        twin_q=config["twin_q"],
        add_layer_norm=(policy.config["exploration_config"].get("type") ==
                        "ParameterNoise"),
    )

    policy.target_model = ModelCatalog.get_model_v2(
        obs_space=observation_space,
        action_space=action_space,
        num_outputs=num_outputs,
        model_config=config["model"],
        framework=config["framework"],
        default_model=MADDPG_RNN_TorchModel,
        name="rnnddpg_model",
        policy_model_config=policy_model_config,
        q_model_config=q_model_config,
        twin_q=config["twin_q"],
        add_layer_norm=(policy.config["exploration_config"].get("type") ==
                        "ParameterNoise"),
    )

    return policy.model


def build_maddpg_models_and_action_dist(
        policy: Policy, obs_space: gym.spaces.Space,
        action_space: gym.spaces.Space,
        config: TrainerConfigDict) -> Tuple[ModelV2, ActionDistribution]:
    model = build_maddpg_models(policy, obs_space, action_space, config)

    assert model.get_initial_state() != [], \
        "RNNDDPG requires its model to be a recurrent one!"

    if isinstance(action_space, Simplex):
        return model, TorchDirichlet
    else:
        return model, TorchDeterministic


class MADDPG_RNN_TorchModel(DDPG_RNN_TorchModel):
    """
    Data flow:
        obs -> forward() -> model_out
        model_out -> get_policy_output() -> pi(s)
        model_out, actions -> get_q_values() -> Q(s, a)
        model_out, actions -> get_twin_q_values() -> Q_twin(s, a)

    Note that this class by itself is not a valid model unless you
    implement forward() in a subclass.
    """

    @override(DDPG_RNN_TorchModel)
    def forward(self, input_dict: Dict[str, TensorType],
                state: List[TensorType],
                seq_lens: TensorType):
        """The common (Q-net and policy-net) forward pass.

        NOTE: It is not(!) recommended to override this method as it would
        introduce a shared pre-network, which would be updated by both
        actor- and critic optimizers.

        For rnn support remove input_dict filter and pass state and seq_lens
        """
        model_out = {"obs": input_dict[SampleBatch.OBS]}
        if "opponent_actions" in input_dict:  # add additional info to model out
            model_out["state"] = input_dict["state"]
            model_out["opponent_actions"] = input_dict["opponent_actions"]
        else:  # haven't gone trough postprocessing
            o = input_dict["obs"]
            if self.state_flag:
                model_out["state"] = torch.zeros_like(o["state"], dtype=o["state"].dtype)
            else:
                model_out["state"] = torch.zeros((o["obs"].shape[0], self.num_agents, o["obs"].shape[1]),
                                                 dtype=o["obs"].dtype)

            if "actions" not in input_dict:
                input_dict["actions"] = input_dict["prev_actions"]
            model_out["opponent_actions"] = torch.stack(
                [torch.zeros_like(input_dict["actions"], dtype=input_dict["actions"].dtype) for _ in
                 range(self.num_agents - 1)], axis=1)

        if self.use_prev_action:
            model_out["prev_actions"] = input_dict[SampleBatch.PREV_ACTIONS]
            model_out["prev_opponent_actions"] = input_dict["prev_opponent_actions"]

        if self.use_prev_reward:
            model_out["prev_rewards"] = input_dict[SampleBatch.PREV_REWARDS]

        return model_out, state

    def _get_cc_q_value(self, model_out: TensorType,
                        state_in: List[TensorType],
                        net,
                        actions,
                        seq_lens: TensorType):
        # Continuous case -> concat actions to model_out.
        model_out = copy.deepcopy(model_out)
        if actions is not None:
            model_out["actions"] = actions
        else:
            actions = torch.zeros(
                list(model_out[SampleBatch.OBS]["obs"].shape[:-1]) + [self.action_dim])
            model_out["actions"] = actions.to(state_in[0].device)

        # Switch on training mode (when getting Q-values, we are usually in
        # training).
        model_out["is_training"] = True

        out, state_out = net(model_out, state_in, seq_lens)
        return out, state_out

    def get_cc_q_values(self,
                        model_out: TensorType,
                        state_in: List[TensorType],
                        seq_lens: TensorType,
                        actions: Optional[TensorType] = None) -> TensorType:
        return self._get_cc_q_value(model_out, state_in, self.q_model, actions,
                                    seq_lens)


# Copied from rnnddpg but optimizing the central q function.
def central_critic_ddpg_loss(policy, model, dist_class, train_batch):
    CentralizedQValueMixin.__init__(policy)
    target_model = policy.target_models[model]

    i = 0
    state_batches = []
    while "state_in_{}".format(i) in train_batch:
        state_batches.append(train_batch["state_in_{}".format(i)])
        i += 1
    assert state_batches
    seq_lens = train_batch.get(SampleBatch.SEQ_LENS)

    twin_q = policy.config["twin_q"]
    gamma = policy.config["gamma"]
    n_step = policy.config["n_step"]
    use_huber = policy.config["use_huber"]
    huber_threshold = policy.config["huber_threshold"]
    l2_reg = policy.config["l2_reg"]

    input_dict = {
        "obs": train_batch[SampleBatch.CUR_OBS],
        "state": train_batch["state"],
        "is_training": True,
        "prev_actions": train_batch[SampleBatch.PREV_ACTIONS],
        "opponent_actions": train_batch["opponent_actions"],
        "prev_opponent_actions": train_batch["prev_opponent_actions"],
        "prev_rewards": train_batch[SampleBatch.PREV_REWARDS],
    }
    model_out_t, state_in_t = model(input_dict, state_batches, seq_lens)
    states_in_t = model.select_state(state_in_t, ["policy", "q", "twin_q"])

    input_dict_next = {
        "obs": train_batch[SampleBatch.NEXT_OBS],
        "state": train_batch["new_state"],
        "is_training": True,
        "prev_actions": train_batch[SampleBatch.ACTIONS],
        "opponent_actions": train_batch["next_opponent_actions"],
        "prev_opponent_actions": train_batch["opponent_actions"],
        "prev_rewards": train_batch[SampleBatch.REWARDS],
    }

    # model_out_tp1, state_in_tp1 = model(
    #     input_dict_next, state_batches, seq_lens)
    # states_in_tp1 = model.select_state(state_in_tp1, ["policy", "q", "twin_q"])

    target_model_out_tp1, target_state_in_tp1 = target_model(
        input_dict_next, state_batches, seq_lens)
    target_states_in_tp1 = target_model.select_state(target_state_in_tp1,
                                                     ["policy", "q", "twin_q"])

    # Policy network evaluation.
    # prev_update_ops = set(tf1.get_collection(tf.GraphKeys.UPDATE_OPS))
    policy_t = model.get_policy_output(
        model_out_t, states_in_t["policy"], seq_lens)[0]
    # policy_batchnorm_update_ops = list(
    #    set(tf1.get_collection(tf.GraphKeys.UPDATE_OPS)) - prev_update_ops)

    policy_tp1 = target_model.get_policy_output(
        target_model_out_tp1, target_states_in_tp1["policy"], seq_lens)[0]

    # Action outputs.
    if policy.config["smooth_target_policy"]:
        target_noise_clip = policy.config["target_noise_clip"]
        clipped_normal_sample = torch.clamp(
            torch.normal(
                mean=torch.zeros(policy_tp1.size()),
                std=policy.config["target_noise"]).to(policy_tp1.device),
            -target_noise_clip, target_noise_clip)

        policy_tp1_smoothed = torch.min(
            torch.max(
                policy_tp1 + clipped_normal_sample,
                torch.tensor(
                    policy.action_space.low,
                    dtype=torch.float32,
                    device=policy_tp1.device)),
            torch.tensor(
                policy.action_space.high,
                dtype=torch.float32,
                device=policy_tp1.device))
    else:
        # No smoothing, just use deterministic actions.
        policy_tp1_smoothed = policy_tp1

    # Q-net(s) evaluation.
    # prev_update_ops = set(tf1.get_collection(tf.GraphKeys.UPDATE_OPS))
    # Q-values for given actions & observations in given current
    q_t = model.get_cc_q_values(
        model_out_t, states_in_t["q"], seq_lens, train_batch[SampleBatch.ACTIONS])[0]

    # Q-values for current policy (no noise) in given current state
    q_t_det_policy = model.get_cc_q_values(
        model_out_t, states_in_t["q"], seq_lens, policy_t)[0]
    q_t_det_policy = torch.squeeze(input=q_t_det_policy, axis=len(q_t_det_policy.shape) - 1)

    if twin_q:
        twin_q_t = model.get_twin_q_values(model_out_t, states_in_t["twin_q"], seq_lens,
                                           train_batch[SampleBatch.ACTIONS])[0]
    # q_batchnorm_update_ops = list(
    #     set(tf1.get_collection(tf.GraphKeys.UPDATE_OPS)) - prev_update_ops)

    # Target q-net(s) evaluation.
    q_tp1 = target_model.get_cc_q_values(
        target_model_out_tp1, target_states_in_tp1["q"], seq_lens, policy_tp1_smoothed)[0]

    if twin_q:
        twin_q_tp1 = target_model.get_twin_q_values(target_model_out_tp1, target_states_in_tp1["twin_q"], seq_lens,
                                                    policy_tp1_smoothed)[0]

    q_t_selected = torch.squeeze(q_t, axis=len(q_t.shape) - 1)
    if twin_q:
        twin_q_t_selected = torch.squeeze(twin_q_t, axis=len(q_t.shape) - 1)
        q_tp1 = torch.min(q_tp1, twin_q_tp1)

    q_tp1_best = torch.squeeze(input=q_tp1, axis=len(q_tp1.shape) - 1)
    q_tp1_best_masked = \
        (1.0 - train_batch[SampleBatch.DONES].float()) * \
        q_tp1_best

    # Compute RHS of bellman equation.
    q_t_selected_target = (train_batch[SampleBatch.REWARDS] +
                           gamma ** n_step * q_tp1_best_masked).detach()

    # BURNIN #
    B = state_batches[0].shape[0]
    T = q_t_selected.shape[0] // B
    seq_mask = sequence_mask(train_batch[SampleBatch.SEQ_LENS], T)
    # Mask away also the burn-in sequence at the beginning.
    burn_in = policy.config["burn_in"]
    if burn_in > 0 and burn_in < T:
        seq_mask[:, :burn_in] = False

    seq_mask = seq_mask.reshape(-1)
    num_valid = torch.sum(seq_mask)

    def reduce_mean_valid(t):
        return torch.sum(t[seq_mask]) / num_valid

    # Compute the error (potentially clipped).
    if twin_q:
        td_error = q_t_selected - q_t_selected_target
        td_error = td_error * seq_mask
        twin_td_error = twin_q_t_selected - q_t_selected_target
        if use_huber:
            errors = huber_loss(td_error, huber_threshold) \
                     + huber_loss(twin_td_error, huber_threshold)
        else:
            errors = 0.5 * \
                     (torch.pow(td_error, 2.0) + torch.pow(twin_td_error, 2.0))
    else:
        td_error = q_t_selected - q_t_selected_target
        td_error = td_error * seq_mask
        if use_huber:
            errors = huber_loss(td_error, huber_threshold)
        else:
            errors = 0.5 * torch.pow(td_error, 2.0)

    critic_loss = torch.mean(train_batch[PRIO_WEIGHTS] * errors)
    actor_loss = -torch.mean(q_t_det_policy * seq_mask)

    # Add l2-regularization if required.
    if l2_reg is not None:
        for name, var in model.policy_variables(as_dict=True).items():
            if "bias" not in name:
                actor_loss += (l2_reg * l2_loss(var))
        for name, var in model.q_variables(as_dict=True).items():
            if "bias" not in name:
                critic_loss += (l2_reg * l2_loss(var))

    # Model self-supervised losses.
    if policy.config["use_state_preprocessor"]:
        # Expand input_dict in case custom_loss' need them.
        input_dict[SampleBatch.ACTIONS] = train_batch[SampleBatch.ACTIONS]
        input_dict[SampleBatch.REWARDS] = train_batch[SampleBatch.REWARDS]
        input_dict[SampleBatch.DONES] = train_batch[SampleBatch.DONES]
        input_dict[SampleBatch.NEXT_OBS] = train_batch[SampleBatch.NEXT_OBS]
        [actor_loss, critic_loss] = model.custom_loss(
            [actor_loss, critic_loss], input_dict)

    # Store values for stats function in model (tower), such that for
    # multi-GPU, we do not override them during the parallel loss phase.
    model.tower_stats["q_t"] = q_t * seq_mask[..., None]
    model.tower_stats["actor_loss"] = actor_loss
    model.tower_stats["critic_loss"] = critic_loss
    # TD-error tensor in final stats
    # will be concatenated and retrieved for each individual batch item.
    model.tower_stats["td_error"] = td_error

    # Return two loss terms (corresponding to the two optimizers, we create).
    return actor_loss, critic_loss


def vf_preds_fetches(policy, input_dict, state_batches, model, action_dist):
    return dict()


MADDPGRNNTorchPolicy = DDPGRNNTorchPolicy.with_updates(
    name="MADDPGRNNTorchPolicy",
    postprocess_fn=centralized_critic_q,
    extra_action_out_fn=vf_preds_fetches,
    make_model_and_action_dist=build_maddpg_models_and_action_dist,
    loss_fn=central_critic_ddpg_loss,
    mixins=[
        TargetNetworkMixin,
        ComputeTDErrorMixin,
        CentralizedQValueMixin
    ]
)


def get_policy_class(config: TrainerConfigDict) -> Optional[Type[Policy]]:
    if config["framework"] == "torch":
        return MADDPGRNNTorchPolicy


def before_learn_on_batch(multi_agent_batch, policies, train_batch_size):

    other_agent_next_action_dict = {}
    all_agent_next_action = []
    for pid, policy in policies.items():

        # get agent number:
        if 0 not in other_agent_next_action_dict:
            custom_config = policy.config["model"]["custom_model_config"]
            n_agents = custom_config["num_agents"]
            for i in range(n_agents):
                other_agent_next_action_dict[i] = []

        policy_batch = multi_agent_batch.policy_batches[pid]
        target_policy_model = policy.target_model.policy_model
        next_obs = policy_batch["new_obs"]

        input_dict = {"obs": {}}
        input_dict["obs"]["obs"] = next_obs

        state_in = policy_batch["state_in_0"]
        seq_lens = policy_batch["seq_lens"]

        input_dict = convert_to_torch_tensor(input_dict, policy.device)
        state_in = convert_to_torch_tensor(state_in, policy.device).unsqueeze(0)
        seq_lens = convert_to_torch_tensor(seq_lens, policy.device)

        next_action_out, _ = target_policy_model.forward(input_dict, state_in, seq_lens)
        next_action = target_policy_model.action_out_squashed(next_action_out)
        next_action = convert_to_numpy(next_action)

        agent_id = np.unique(policy_batch["agent_index"])
        for a_id in agent_id:
            valid_flag = np.where(policy_batch["agent_index"] == a_id)[0]
            next_action_one_agent = next_action[valid_flag, :]
            all_agent_next_action.append(next_action_one_agent)
            for key in other_agent_next_action_dict.keys():
                if key != a_id:
                    other_agent_next_action_dict[a_id].append(next_action_one_agent)

    # construct opponent next action for each batch
    all_agent_next_action = np.stack(all_agent_next_action, 1)
    for pid, policy in policies.items():
        policy_batch = multi_agent_batch.policy_batches[pid]
        agent_id = np.unique(policy_batch["agent_index"])
        agent_num = len(agent_id)
        ls = []
        for a in range(agent_num):
            ls.append(all_agent_next_action)
        next_action_batch = np.stack(ls, 1).reshape((policy_batch.count, n_agents, -1))
        other_next_action_batch_ls = []
        for i in range(policy_batch.count):
            current_agent_id = policy_batch["agent_index"][i]
            next_action_ts = next_action_batch[i]
            other_next_action_ts = np.delete(next_action_ts, current_agent_id, axis=0)
            other_next_action_batch_ls.append(other_next_action_ts)
        other_next_action_batch = np.stack(other_next_action_batch_ls, 0)
        multi_agent_batch.policy_batches[pid]["next_opponent_actions"] = other_next_action_batch

    return multi_agent_batch


def validate_config(config: TrainerConfigDict) -> None:
    # Add the `burn_in` to the Model's max_seq_len.
    # Set the replay sequence length to the max_seq_len of the model.
    config["replay_sequence_length"] = \
        config["burn_in"] + config["model"]["max_seq_len"]

    def f(batch, workers, config):
        policies = dict(workers.local_worker()
                        .foreach_trainable_policy(lambda p, i: (i, p)))
        return before_learn_on_batch(batch, policies,
                                     config["train_batch_size"])

    config["before_learn_on_batch"] = f


MADDPGRNNTrainer = DDPGRNNTrainer.with_updates(
    name="MADDPGRNNTrainer",
    default_config=DDPG_RNN_DEFAULT_CONFIG,
    default_policy=MADDPGRNNTorchPolicy,
    get_policy_class=get_policy_class,
    validate_config=validate_config,
    allow_unknown_subkeys=["Q_model", "policy_model"]
)
