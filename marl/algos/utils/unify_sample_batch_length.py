from typing import List, Optional
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.utils.typing import ViewRequirementsDict
import numpy as np
from ray.rllib.utils.annotations import DeveloperAPI
from ray.util import log_once
import logging
from ray.rllib.utils.debug import summarize

logger = logging.getLogger(__name__)


@DeveloperAPI
def chop_into_sequences(*,
                        feature_columns,
                        state_columns,
                        max_seq_len,
                        episode_ids=None,
                        unroll_ids=None,
                        agent_indices=None,
                        dynamic_max=True,
                        shuffle=False,
                        seq_lens=None,
                        states_already_reduced_to_init=False,
                        _extra_padding=0):
    """Truncate and pad experiences into fixed-length sequences.

    Args:
        feature_columns (list): List of arrays containing features.
        state_columns (list): List of arrays containing LSTM state values.
        max_seq_len (int): Max length of sequences before truncation.
        episode_ids (List[EpisodeID]): List of episode ids for each step.
        unroll_ids (List[UnrollID]): List of identifiers for the sample batch.
            This is used to make sure sequences are cut between sample batches.
        agent_indices (List[AgentID]): List of agent ids for each step. Note
            that this has to be combined with episode_ids for uniqueness.
        dynamic_max (bool): Whether to dynamically shrink the max seq len.
            For example, if max len is 20 and the actual max seq len in the
            data is 7, it will be shrunk to 7.
        shuffle (bool): Whether to shuffle the sequence outputs.
        _extra_padding (int): Add extra padding to the end of sequences.

    Returns:
        f_pad (list): Padded feature columns. These will be of shape
            [NUM_SEQUENCES * MAX_SEQ_LEN, ...].
        s_init (list): Initial states for each sequence, of shape
            [NUM_SEQUENCES, ...].
        seq_lens (list): List of sequence lengths, of shape [NUM_SEQUENCES].

    Examples:
        >>> f_pad, s_init, seq_lens = chop_into_sequences(
                episode_ids=[1, 1, 5, 5, 5, 5],
                unroll_ids=[4, 4, 4, 4, 4, 4],
                agent_indices=[0, 0, 0, 0, 0, 0],
                feature_columns=[[4, 4, 8, 8, 8, 8],
                                 [1, 1, 0, 1, 1, 0]],
                state_columns=[[4, 5, 4, 5, 5, 5]],
                max_seq_len=3)
        >>> print(f_pad)
        [[4, 4, 0, 8, 8, 8, 8, 0, 0],
         [1, 1, 0, 0, 1, 1, 0, 0, 0]]
        >>> print(s_init)
        [[4, 4, 5]]
        >>> print(seq_lens)
        [2, 3, 1]
    """

    if seq_lens is None or len(seq_lens) == 0:
        prev_id = None
        seq_lens = []
        seq_len = 0
        unique_ids = np.add(
            np.add(episode_ids, agent_indices),
            np.array(unroll_ids, dtype=np.int64) << 32)
        for uid in unique_ids:
            if (prev_id is not None and uid != prev_id) or \
                    seq_len >= max_seq_len:
                seq_lens.append(seq_len)
                seq_len = 0
            seq_len += 1
            prev_id = uid
        if seq_len:
            seq_lens.append(seq_len)
        seq_lens = np.array(seq_lens, dtype=np.int32)

    assert sum(seq_lens) == len(feature_columns[0])

    # Dynamically shrink max len as needed to optimize memory usage
    if dynamic_max:
        max_seq_len = max(seq_lens) + _extra_padding

    feature_sequences = []
    for f in feature_columns:
        # Save unnecessary copy.
        if not isinstance(f, np.ndarray):
            f = np.array(f)
        length = len(seq_lens) * max_seq_len
        if f.dtype == np.object or f.dtype.type is np.str_:
            f_pad = [None] * length
        else:
            # Make sure type doesn't change.
            f_pad = np.zeros((length, ) + np.shape(f)[1:], dtype=f.dtype)
        seq_base = 0
        i = 0
        for len_ in seq_lens:
            for seq_offset in range(len_):
                f_pad[seq_base + seq_offset] = f[i]
                i += 1
            seq_base += max_seq_len

        if i != len(f): f = f[:i]

        assert i == len(f), f
        feature_sequences.append(f_pad)

    if states_already_reduced_to_init:
        initial_states = state_columns
    else:
        initial_states = []
        for s in state_columns:
            # Skip unnecessary copy.
            if not isinstance(s, np.ndarray):
                s = np.array(s)
            s_init = []
            i = 0
            for len_ in seq_lens:
                s_init.append(s[i])
                i += len_
            initial_states.append(np.array(s_init))

    if shuffle:
        permutation = np.random.permutation(len(seq_lens))
        for i, f in enumerate(feature_sequences):
            orig_shape = f.shape
            f = np.reshape(f, (len(seq_lens), -1) + f.shape[1:])
            f = f[permutation]
            f = np.reshape(f, orig_shape)
            feature_sequences[i] = f
        for i, s in enumerate(initial_states):
            s = s[permutation]
            initial_states[i] = s
        seq_lens = seq_lens[permutation]

    return feature_sequences, initial_states, seq_lens


@DeveloperAPI
def pad_batch_to_sequences_of_same_size(
        batch: SampleBatch,
        max_seq_len: int,
        shuffle: bool = False,
        batch_divisibility_req: int = 1,
        feature_keys: Optional[List[str]] = None,
        view_requirements: Optional[ViewRequirementsDict] = None,
):
    """Applies padding to `batch` so it's choppable into same-size sequences.

    Shuffles `batch` (if desired), makes sure divisibility requirement is met,
    then pads the batch ([B, ...]) into same-size chunks ([B, ...]) w/o
    adding a time dimension (yet).
    Padding depends on episodes found in batch and `max_seq_len`.

    Args:
        batch (SampleBatch): The SampleBatch object. All values in here have
            the shape [B, ...].
        max_seq_len (int): The max. sequence length to use for chopping.
        shuffle (bool): Whether to shuffle batch sequences. Shuffle may
            be done in-place. This only makes sense if you're further
            applying minibatch SGD after getting the outputs.
        batch_divisibility_req (int): The int by which the batch dimension
            must be dividable.
        feature_keys (Optional[List[str]]): An optional list of keys to apply
            sequence-chopping to. If None, use all keys in batch that are not
            "state_in/out_"-type keys.
        view_requirements (Optional[ViewRequirementsDict]): An optional
            Policy ViewRequirements dict to be able to infer whether
            e.g. dynamic max'ing should be applied over the seq_lens.
    """
    if batch.zero_padded:
        return

    batch.zero_padded = True

    if batch_divisibility_req > 1:
        meets_divisibility_reqs = (
            len(batch[SampleBatch.CUR_OBS]) % batch_divisibility_req == 0
            # not multiagent
            and max(batch[SampleBatch.AGENT_INDEX]) == 0)
    else:
        meets_divisibility_reqs = True

    states_already_reduced_to_init = False

    # RNN/attention net case. Figure out whether we should apply dynamic
    # max'ing over the list of sequence lengths.
    if "state_in_0" in batch or "state_out_0" in batch:
        # Check, whether the state inputs have already been reduced to their
        # init values at the beginning of each max_seq_len chunk.
        if batch.get(SampleBatch.SEQ_LENS) is not None and \
                len(batch["state_in_0"]) == len(batch[SampleBatch.SEQ_LENS]):
            states_already_reduced_to_init = True

        # RNN (or single timestep state-in): Set the max dynamically.
        if view_requirements and view_requirements["state_in_0"].shift_from is None:
            dynamic_max = True
        # Attention Nets (state inputs are over some range): No dynamic maxing possible.
        else:
            dynamic_max = False
    # Multi-agent case.
    elif not meets_divisibility_reqs:
        max_seq_len = batch_divisibility_req
        dynamic_max = False
        batch.max_seq_len = max_seq_len
    # Simple case: No RNN/attention net, nor do we need to pad.
    else:
        if shuffle:
            batch.shuffle()
        return

    # RNN, attention net, or multi-agent case.
    state_keys = []
    feature_keys_ = feature_keys or []
    for k, v in batch.items():
        if k.startswith("state_in_"):
            state_keys.append(k)
        elif not feature_keys and not k.startswith("state_out_") and \
                k not in ["infos", SampleBatch.SEQ_LENS] and \
                isinstance(v, np.ndarray):
            feature_keys_.append(k)

    feature_sequences, initial_states, seq_lens = \
        chop_into_sequences(
            feature_columns=[batch[k] for k in feature_keys_],
            state_columns=[batch[k] for k in state_keys],
            episode_ids=batch.get(SampleBatch.EPS_ID),
            unroll_ids=batch.get(SampleBatch.UNROLL_ID),
            agent_indices=batch.get(SampleBatch.AGENT_INDEX),
            seq_lens=batch.get(SampleBatch.SEQ_LENS),
            max_seq_len=max_seq_len,
            dynamic_max=dynamic_max,
            states_already_reduced_to_init=states_already_reduced_to_init,
            shuffle=shuffle)

    for i, k in enumerate(feature_keys_):
        batch[k] = feature_sequences[i]
    for i, k in enumerate(state_keys):
        batch[k] = initial_states[i]
    batch[SampleBatch.SEQ_LENS] = np.array(seq_lens)
    if dynamic_max:
    	batch.max_seq_len = max(seq_lens)

    if log_once("rnn_ma_feed_dict"):
        logger.info("Padded input for RNN/Attn.Nets/MA:\n\n{}\n".format(
            summarize({
                "features": feature_sequences,
                "initial_states": initial_states,
                "seq_lens": seq_lens,
                "max_seq_len": max_seq_len,
            })))

