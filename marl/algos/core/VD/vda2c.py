from ray.rllib.models.action_dist import ActionDistribution
from ray.rllib.evaluation.postprocessing import compute_gae_for_sample_batch, \
    Postprocessing, compute_advantages
from ray.rllib.models.modelv2 import ModelV2
from ray.rllib.policy.policy import Policy
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.utils.framework import try_import_torch
from ray.rllib.utils.torch_ops import apply_grad_clipping, \
    explained_variance, sequence_mask
from ray.rllib.utils.typing import TensorType, TrainerConfigDict
from ray.rllib.utils.torch_ops import convert_to_torch_tensor
from ray.rllib.agents.a3c.a3c_torch_policy import A3CTorchPolicy
from ray.rllib.agents.a3c.a2c import A2C_DEFAULT_CONFIG as A2C_CONFIG, A2CTrainer
from ray.rllib.agents.ppo.ppo_torch_policy import PPOTorchPolicy, KLCoeffMixin, ValueNetworkMixin
from marl.algos.utils.postprocessing import MixingValueMixin, value_mixing_postprocessing

torch, nn = try_import_torch()


# value decomposition based actor critic loss
def value_mix_actor_critic_loss(policy: Policy, model: ModelV2,
                                dist_class: ActionDistribution,
                                train_batch: SampleBatch) -> TensorType:
    MixingValueMixin.__init__(policy)

    logits, _ = model.from_batch(train_batch)
    values = model.value_function()

    # add mixing_function
    opponent_vf_preds = convert_to_torch_tensor(train_batch["opponent_vf_preds"])
    vf_pred = values.unsqueeze(1)
    all_vf_pred = torch.cat((vf_pred, opponent_vf_preds), 1)
    state = convert_to_torch_tensor(train_batch["state"])
    value_tot = model.mixing_value(all_vf_pred, state)

    if policy.is_recurrent():
        B = len(train_batch[SampleBatch.SEQ_LENS])
        max_seq_len = logits.shape[0] // B
        mask_orig = sequence_mask(train_batch[SampleBatch.SEQ_LENS],
                                  max_seq_len)
        valid_mask = torch.reshape(mask_orig, [-1])
    else:
        valid_mask = torch.ones_like(value_tot, dtype=torch.bool)

    dist = dist_class(logits, model)
    log_probs = dist.logp(train_batch[SampleBatch.ACTIONS]).reshape(-1)
    pi_err = -torch.sum(
        torch.masked_select(log_probs * train_batch[Postprocessing.ADVANTAGES],
                            valid_mask))

    # Compute a value function loss.
    if policy.config["use_critic"]:
        value_err = 0.5 * torch.sum(
            torch.pow(
                torch.masked_select(
                    value_tot.reshape(-1) -
                    train_batch[Postprocessing.VALUE_TARGETS], valid_mask),
                2.0))
    # Ignore the value function.
    else:
        value_err = 0.0

    entropy = torch.sum(torch.masked_select(dist.entropy(), valid_mask))

    total_loss = (pi_err + value_err * policy.config["vf_loss_coeff"] -
                  entropy * policy.config["entropy_coeff"])

    # Store values for stats function in model (tower), such that for
    # multi-GPU, we do not override them during the parallel loss phase.
    model.tower_stats["entropy"] = entropy
    model.tower_stats["pi_err"] = pi_err
    model.tower_stats["value_err"] = value_err

    return total_loss


VDA2CTorchPolicy = A3CTorchPolicy.with_updates(
    name="VDA2CTorchPolicy",
    get_default_config=lambda: A2C_CONFIG,
    postprocess_fn=value_mixing_postprocessing,
    loss_fn=value_mix_actor_critic_loss,
    mixins=[ValueNetworkMixin, MixingValueMixin],
)


def get_policy_class_vda2c(config_):
    if config_["framework"] == "torch":
        return VDA2CTorchPolicy


VDA2CTrainer = A2CTrainer.with_updates(
    name="VDA2CTrainer",
    default_policy=None,
    get_policy_class=get_policy_class_vda2c,
)
