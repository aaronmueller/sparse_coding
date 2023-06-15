import argparse
import torch

from utils import dotdict

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--use_wandb", type=bool, default=True)
    parser.add_argument("--n_ground_truth_components", type=int, default=512)
    parser.add_argument("--learned_dict_ratio", type=float, default=1.0)
    parser.add_argument("--max_length", type=int, default=256)  # when tokenizing, truncate to this length, basically the context size
    parser.add_argument("--load_autoencoders", type=str, default="")

    parser.add_argument("--model_batch_size", type=int, default=4)
    parser.add_argument("--batch_size", type=int, default=256)
    parser.add_argument("--noise_std", type=float, default=0.1)
    parser.add_argument("--l1_alpha", type=float, default=0.1)
    parser.add_argument("--learning_rate", type=float, default=0.001)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--noise_level", type=float, default=0.0)

    parser.add_argument("--feature_prob_decay", type=float, default=0.99)
    parser.add_argument("--feature_num_nonzero", type=int, default=5)
    parser.add_argument("--correlated_components", type=bool, default=True)

    parser.add_argument("--l1_exp_low", type=int, default=-12)
    parser.add_argument("--l1_exp_high", type=int, default=-11)  # not inclusive
    parser.add_argument("--l1_exp_base", type=float, default=10 ** (1 / 4))
    parser.add_argument("--dict_ratio_exp_low", type=int, default=1)
    parser.add_argument("--dict_ratio_exp_high", type=int, default=7)  # not inclusive
    parser.add_argument("--dict_ratio_exp_base", type=int, default=2)

    parser.add_argument("--run_toy", type=bool, default=False)
    parser.add_argument("--model_name", type=str, default="nanoGPT")
    parser.add_argument("--model_path", type=str, default="models/32d70k.pt")
    parser.add_argument("--dataset_name", type=str, default="NeelNanda/pile-10k")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--layer", type=int, default=2)  # layer to extract mlp-post-non-lin features from, only if using real model

    parser.add_argument("--outputs_folder", type=str, default="outputs")
    parser.add_argument("--datasets_folder", type=str, default="activation_data")
    parser.add_argument("--n_chunks", type=int, default=30)
    parser.add_argument("--threshold", type=float, default=0.9)  # When looking for matching features across dicts, what is the threshold for a match
    parser.add_argument("--max_batches", type=int, default=0)  # How many batches to run the inner loop for before cutting out, 0 means run all
    parser.add_argument("--mini_runs", type=int, default=1)  # How many times to run the inner loop, each time with a different random subset of the data
    args = parser.parse_args()
    cfg = dotdict(vars(args))  # convert to dotdict via dict
    cfg.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    return cfg