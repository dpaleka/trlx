# Trains a model to encode and decode a number in a poem.

import os
import yaml

import yaml
import trlx
from typing import List
from trlx.data.configs import TRLConfig
from trlx.data.ppo_types import RunElementBatch
import transformers
from transformers.tokenization_utils_base import BatchEncoding
import numpy as np
import torch
import pandas as pd

default_config = yaml.safe_load(open("configs/ppo_config.yml"))

def get_last_digit(sample: List[str]) -> int:
    """
    Extract last char from a sample, check if it's a digit, otherwise return -1
    """
    last_word = sample[-1]
    if last_word.isdigit():
        return int(last_word)
    else:
        return -1

def reward_fn(trajectories: List[List]) -> List[float]:
    """
    trajectories is a list of lists having the form [digit, prompt_1, output_1, prompt_2, output_2]
    Return if the last digit of output_2 is the same as the digit
    """
    for sample in trajectories:
        assert len(sample) == 3
    reconstructed_digits = list(map(get_last_digit, trajectories[:, 2]))
    return [1 if digit == reconstructed_digit else 0 for digit, reconstructed_digit in zip(trajectories[:, 0], reconstructed_digits)]


def encoder_decoder_experience_fn(trainer, batch):
    """
    :trainer: AccelerateRLTrainer
        (has trainer.orch.generate_and_calc_logprobs, which returns data : RunElementBatch and stats : dict)
        (has trainer.decode)
    :batch: an object that has .input_ids, .attention_mask, .labels
    
    :return: trajectories (the input type to reward_fn), 
             data : RunElementBatch
             stats
            
    Use model.generate_and_calc_logprobs to return all data needed for PPO for a complex trajectory.
    model.generate_and_calc_logprobs : (batch) 
       -> {'samples': samples, 'all_logprobs': all_logprobs, 'all_ref_logprobs': all_ref_logprobs, 'query_tensors': query_tensors, 'response_tensors': response_tensors, 'all_values': all_values}
    The trajectory for each poem is as follows:
    Sample a digit from {0, 1}.
    First run:
    f"Fact: x = {digit}
    Continue the poem:\n
    {poem}
    The"
    --> poem_continuation
    Second run:
    f"{poem}
    The{poem_continuation}\n
    Recall fact: x is either 0 or 1.
    Answer: x ="
    --> answer
    """

    # batch is an object that has .input_ids, .attention_mask, .labels; for example
    # batch {'input_ids': tensor([[ 3], [ 8]]), 'attention_mask': tensor([[1], [1]]), 'labels': tensor([[ 8], [10]])}

    batch_size = batch.input_ids.shape[0]
    device = batch.input_ids.device
    query_tensors = batch.input_ids
    digits = list(np.random.randint(0, 2, batch_size)) # sample a digit from {0, 1}

    # The key architectural constraint is that alll trainer.orch.generate_and_calc_logprobs should be parallel over the batch
    # Do everything in string space 
    fact_strs = [f"Fact: x = {digits[i]}\nContinue the poem:\n\n" for i in range(batch_size)]
    recall_str = f"\nRecall fact: x is either 0 or 1.\nAnswer: x ="

    # Detokenize the text
    _, str_prompts, _ = trainer.decode(
        query_tensors, query_tensors, # this is a hack to get the text, we are doing redundant tokenization
    )

    first_run_strs = [""] * batch_size
    # First run
    for i in range(batch_size):
        first_run_strs[i] = fact_strs[i] + str_prompts[i] + "The\n"

    # Encode the first run
    first_run_batch = trainer.tokenizer(first_run_strs)

    # Generate the first run
    first_run_data, first_run_stats = trainer.orch.generate_and_calc_logprobs(first_run_batch)

    # Decode the first run
    _, first_run_str_prompts, first_run_str_outputs = trainer.decode(
        first_run_data.query_tensors, first_run_data.response_tensors,  # this one is not intended to be a hack
    )

    # Second run
    second_run_strs = [""] * batch_size
    for i in range(batch_size):
        second_run_strs[i] = str_prompts[i] + "\nThe" + first_run_str_outputs[i] + recall_str

    # Encode the second run
    second_run_batch = trainer.tokenizer(second_run_strs) 

    # Generate the second run
    second_run_data, second_run_stats = trainer.orch.generate_and_calc_logprobs(second_run_batch)

    # Decode the second run
    _, second_run_str_prompts, second_run_str_outputs = trainer.decode(
        second_run_data.query_tensors, second_run_data.response_tensors,  # this one is not intended to be a hack
    )

    # RunElementBatch has an __add__ method which should do the right thing
    data = first_run_data + second_run_data
    # sum up a dict over keys 
    stats = {k: first_run_stats[k] + second_run_stats[k] for k in first_run_stats}

    trajectories = [] # list of lists, of the form [digit, prompt_1, output_1, prompt_2, output_2]
    # convert to a list of lists
    for i in range(batch_size):
        trajectories.append([digits[i], first_run_str_prompts[i], first_run_str_outputs[i], second_run_str_prompts[i], second_run_str_outputs[i]])

    return trajectories, data, stats





def main(hparams={}):
    config = TRLConfig.update(default_config, hparams)

    if torch.cuda.is_available():
        device = int(os.environ.get("LOCAL_RANK", 0))
    else:
        device = -1

    train_path = "examples/experience_demo/poems/poetry_big_train_qa.csv"
    data = pd.read_csv(train_path)
    prompts = data["question"].tolist() # everthing else goes in the trajectory function

    np.random.seed(42)
    trlx.train(
        reward_fn=reward_fn,
        experience_fn=encoder_decoder_experience_fn,
        prompts=prompts,
        config=config,
    )


if __name__ == "__main__":
    main()
