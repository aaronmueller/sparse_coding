import argparse
from collections.abc import Generator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import importlib
import itertools
import json
import math
import multiprocessing as mp
import os
import pickle
from typing import Union, Tuple, List, Any, Optional, TypeVar, Dict

from baukit import Trace
from datasets import Dataset, DatasetDict, load_dataset
from einops import rearrange
from matplotlib import pyplot as plt
import pandas as pd
import numpy as np
import numpy.typing as npt
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchtyping import TensorType
from tqdm import tqdm
from transformer_lens import HookedTransformer
from transformers import PreTrainedTokenizerBase, GPT2Tokenizer
import wandb

from utils import *
from argparser import parse_args
from nanoGPT_model import GPT

n_ground_truth_components, activation_dim, dataset_size = None, None, None
T = TypeVar("T", bound=Union[Dataset, DatasetDict])

def read_from_pile(address: str, max_lines: int = 100_000, start_line: int = 0):
    """Reads a file from the Pile dataset. Returns a generator."""
    
    with open(address, "r") as f:
        for i, line in enumerate(f):
            if i < start_line:
                continue
            if i >= max_lines + start_line:
                break
            yield json.loads(line)


def make_sentence_dataset(dataset_name: str, max_lines: int = 20_000, start_line: int = 0):
    """Returns a dataset from the Huggingface Datasets library."""
    if dataset_name == "EleutherAI/pile":
        if not os.path.exists("pile0"):
            print("Downloading shard 0 of the Pile dataset (requires 50GB of disk space).")
            if not os.path.exists("pile0.zst"):
                os.system("curl https://the-eye.eu/public/AI/pile/train/00.jsonl.zst > pile0.zst")
                os.system("unzstd pile0.zst")
        dataset = Dataset.from_list(list(read_from_pile("pile0", max_lines=max_lines, start_line=start_line)))
    else:
        dataset = load_dataset(dataset_name, split="train")
    return dataset


# Nora's Code from https://github.com/AlignmentResearch/tuned-lens/blob/main/tuned_lens/data.py
def chunk_and_tokenize(
    data: T,
    tokenizer: PreTrainedTokenizerBase,
    *,
    format: str = "torch",
    num_proc: int = min(mp.cpu_count() // 2, 8),
    text_key: str = "text",
    max_length: int = 2048,
    return_final_batch: bool = False,
    load_from_cache_file: bool = True,
) -> tuple[T, float]:
    """Perform GPT-style chunking and tokenization on a dataset.

    The resulting dataset will consist entirely of chunks exactly `max_length` tokens
    long. Long sequences will be split into multiple chunks, and short sequences will
    be merged with their neighbors, using `eos_token` as a separator. The fist token
    will also always be an `eos_token`.

    Args:
        data: The dataset to chunk and tokenize.
        tokenizer: The tokenizer to use.
        format: The format to return the dataset in, passed to `Dataset.with_format`.
        num_proc: The number of processes to use for tokenization.
        text_key: The key in the dataset to use as the text to tokenize.
        max_length: The maximum length of a batch of input ids.
        return_final_batch: Whether to return the final batch, which may be smaller
            than the others.
        load_from_cache_file: Whether to load from the cache file.

    Returns:
        * The chunked and tokenized dataset.
        * The ratio of nats to bits per byte see https://arxiv.org/pdf/2101.00027.pdf,
            section 3.1.
    """

    def _tokenize_fn(x: dict[str, list]):
        chunk_size = min(tokenizer.model_max_length, max_length)  # tokenizer max length is 1024 for gpt2
        sep = tokenizer.eos_token or "<|endoftext|>"
        joined_text = sep.join([""] + x[text_key])
        output = tokenizer(
            # Concatenate all the samples together, separated by the EOS token.
            joined_text,  # start with an eos token
            max_length=chunk_size,
            return_attention_mask=False,
            return_overflowing_tokens=True,
            truncation=True,
        )

        if overflow := output.pop("overflowing_tokens", None):
            # Slow Tokenizers return unnested lists of ints
            assert isinstance(output["input_ids"][0], int)

            # Chunk the overflow into batches of size `chunk_size`
            chunks = [output["input_ids"]] + [overflow[i * chunk_size : (i + 1) * chunk_size] for i in range(math.ceil(len(overflow) / chunk_size))]
            output = {"input_ids": chunks}

        total_tokens = sum(len(ids) for ids in output["input_ids"])
        total_bytes = len(joined_text.encode("utf-8"))

        if not return_final_batch:
            # We know that the last sample will almost always be less than the max
            # number of tokens, and we don't want to pad, so we just drop it.
            output = {k: v[:-1] for k, v in output.items()}

        output_batch_size = len(output["input_ids"])

        if output_batch_size == 0:
            raise ValueError("Not enough data to create a single batch complete batch." " Either allow the final batch to be returned," " or supply more data.")

        # We need to output this in order to compute the number of bits per byte
        div, rem = divmod(total_tokens, output_batch_size)
        output["length"] = [div] * output_batch_size
        output["length"][-1] += rem

        div, rem = divmod(total_bytes, output_batch_size)
        output["bytes"] = [div] * output_batch_size
        output["bytes"][-1] += rem

        return output

    data = data.map(
        _tokenize_fn,
        # Batching is important for ensuring that we don't waste tokens
        # since we always throw away the last element of the batch we
        # want to keep the batch size as large as possible
        batched=True,
        batch_size=2048,
        num_proc=num_proc,
        remove_columns=get_columns_all_equal(data),
        load_from_cache_file=load_from_cache_file,
    )
    total_bytes: float = sum(data["bytes"])
    total_tokens: float = sum(data["length"])
    return data.with_format(format, columns=["input_ids"]), (total_tokens / total_bytes) / math.log(2)


def get_columns_all_equal(dataset: Union[Dataset, DatasetDict]) -> list[str]:
    """Get a single list of columns in a `Dataset` or `DatasetDict`.

    We assert the columms are the same across splits if it's a `DatasetDict`.

    Args:
        dataset: The dataset to get the columns from.

    Returns:
        A list of columns.
    """
    if isinstance(dataset, DatasetDict):
        cols_by_split = dataset.column_names.values()
        columns = next(iter(cols_by_split))
        if not all(cols == columns for cols in cols_by_split):
            raise ValueError("All splits must have the same columns")

        return columns

    return dataset.column_names


# End Nora's Code from https://github.com/AlignmentResearch/tuned-lens/blob/main/tuned_lens/data.py


@dataclass
class RandomDatasetGenerator(Generator):
    activation_dim: int
    n_ground_truth_components: int
    batch_size: int
    feature_num_nonzero: int
    feature_prob_decay: float
    correlated: bool
    device: Union[torch.device, str]

    frac_nonzero: float = field(init=False)
    decay: TensorType["n_ground_truth_components"] = field(init=False)
    feats: TensorType["n_ground_truth_components", "activation_dim"] = field(init=False)
    corr_matrix: Optional[TensorType["n_ground_truth_components", "n_ground_truth_components"]] = field(init=False)
    component_probs: Optional[TensorType["n_ground_truth_components"]] = field(init=False)

    def __post_init__(self): # __post_init__ used so as to not overwrite the init generated by dataclass
        self.frac_nonzero = self.feature_num_nonzero / self.n_ground_truth_components

        # Define the probabilities of each component being included in the data
        self.decay = torch.tensor([self.feature_prob_decay**i for i in range(self.n_ground_truth_components)]).to(self.device)  # FIXME: 1 / i

        if self.correlated:
            self.corr_matrix = generate_corr_matrix(self.n_ground_truth_components, device=self.device)
        else:
            self.component_probs = self.decay * self.frac_nonzero  # Only if non-correlated
        self.feats = generate_rand_feats(
            self.activation_dim,
            self.n_ground_truth_components,
            device=self.device,
        )
        self.t_type = torch.float32

    def send(self, ignored_arg: Any) -> TensorType["dataset_size", "activation_dim"]:
        if self.correlated:
            _, _, data = generate_correlated_dataset(
                self.n_ground_truth_components,
                self.batch_size,
                self.corr_matrix,
                self.feats,
                self.frac_nonzero,
                self.decay,
                self.device,
            )
        else:
            _, _, data = generate_rand_dataset(
                self.n_ground_truth_components,
                self.batch_size,
                self.component_probs,
                self.feats,
                self.device,
            )
        return data.to(self.t_type)

    def throw(self, type: Any = None, value: Any = None, traceback: Any = None) -> None:
        raise StopIteration


def generate_rand_dataset(
    n_ground_truth_components: int,  #
    dataset_size: int,
    feature_probs: TensorType["n_ground_truth_components"],
    feats: TensorType["n_ground_truth_components", "activation_dim"],
    device: Union[torch.device, str],
) -> Tuple[TensorType["n_ground_truth_components", "activation_dim"], TensorType["dataset_size", "n_ground_truth_components"], TensorType["dataset_size", "activation_dim"]]:
    dataset_thresh = torch.rand(dataset_size, n_ground_truth_components, device=device)
    dataset_values = torch.rand(dataset_size, n_ground_truth_components, device=device)

    data_zero = torch.zeros_like(dataset_thresh, device=device)

    dataset_codes = torch.where(
        dataset_thresh <= feature_probs,
        dataset_values,
        data_zero,
    )  # dim: dataset_size x n_ground_truth_components

    # Multiply by a 2D random matrix of feature strengths
    feature_strengths = torch.rand((dataset_size, n_ground_truth_components), device=device)
    dataset = (dataset_codes * feature_strengths) @ feats

    # dataset = dataset_codes @ feats

    return feats, dataset_codes, dataset


def generate_correlated_dataset(
    n_ground_truth_components: int,
    dataset_size: int,
    corr_matrix: TensorType["n_ground_truth_components", "n_ground_truth_components"],
    feats: TensorType["n_ground_truth_components", "activation_dim"],
    frac_nonzero: float,
    decay: TensorType["n_ground_truth_components"],
    device: Union[torch.device, str],
) -> Tuple[TensorType["n_ground_truth_components", "activation_dim"], TensorType["dataset_size", "n_ground_truth_components"], TensorType["dataset_size", "activation_dim"]]:
    # Get a correlated gaussian sample
    mvn = torch.distributions.MultivariateNormal(loc=torch.zeros(n_ground_truth_components, device=device), covariance_matrix=corr_matrix)
    corr_thresh = mvn.sample()

    # Take the CDF of that sample.
    normal = torch.distributions.Normal(torch.tensor([0.0], device=device), torch.tensor([1.0], device=device))
    cdf = normal.cdf(corr_thresh.squeeze())

    # Decay it
    component_probs = cdf * decay

    # Scale it to get the right % of nonzeros
    mean_prob = torch.mean(component_probs)
    scaler = frac_nonzero / mean_prob
    component_probs *= scaler
    # So np.isclose(np.mean(component_probs), frac_nonzero) will be True

    # Generate sparse correlated codes
    dataset_thresh = torch.rand(dataset_size, n_ground_truth_components, device=device)
    dataset_values = torch.rand(dataset_size, n_ground_truth_components, device=device)

    data_zero = torch.zeros_like(corr_thresh, device=device)
    dataset_codes = torch.where(
        dataset_thresh <= component_probs,
        dataset_values,
        data_zero,
    )
    # Ensure there are no datapoints w/ 0 features
    zero_sample_index = (dataset_codes.count_nonzero(dim=1) == 0).nonzero()[:, 0]
    random_index = torch.randint(low=0, high=n_ground_truth_components, size=(zero_sample_index.shape[0],)).to(dataset_codes.device)
    dataset_codes[zero_sample_index, random_index] = 1.0

    # Multiply by a 2D random matrix of feature strengths
    feature_strengths = torch.rand((dataset_size, n_ground_truth_components), device=device)
    dataset = (dataset_codes * feature_strengths) @ feats

    return feats, dataset_codes, dataset


def generate_rand_feats(
    feat_dim: int,
    num_feats: int,
    device: Union[torch.device, str],
) -> TensorType["n_ground_truth_components", "activation_dim"]:
    data_path = os.path.join(os.getcwd(), "data")
    data_filename = os.path.join(data_path, f"feats_{feat_dim}_{num_feats}.npy")

    feats = np.random.multivariate_normal(np.zeros(feat_dim), np.eye(feat_dim), size=num_feats)
    feats = feats.T / np.linalg.norm(feats, axis=1)
    feats = feats.T

    feats_tensor = torch.from_numpy(feats).to(device).float()
    return feats_tensor


def generate_corr_matrix(num_feats: int, device: Union[torch.device, str]) -> TensorType["n_ground_truth_components", "n_ground_truth_components"]:
    corr_mat_path = os.path.join(os.getcwd(), "data")
    corr_mat_filename = os.path.join(corr_mat_path, f"corr_mat_{num_feats}.npy")

    # Create a correlation matrix
    corr_matrix = np.random.rand(num_feats, num_feats)
    corr_matrix = (corr_matrix + corr_matrix.T) / 2
    min_eig = np.min(np.real(np.linalg.eigvals(corr_matrix)))
    if min_eig < 0:
        corr_matrix -= 1.001 * min_eig * np.eye(corr_matrix.shape[0], corr_matrix.shape[1])

    corr_matrix_tensor = torch.from_numpy(corr_matrix).to(device).float()

    return corr_matrix_tensor


# AutoEncoder Definition
class AutoEncoder(nn.Module):
    def __init__(self, activation_size, n_dict_components, t_type=torch.float32, l1_coef=0.0):
        super(AutoEncoder, self).__init__()
        self.decoder = nn.Linear(n_dict_components, activation_size, bias=False)
        # Initialize the decoder weights orthogonally
        nn.init.orthogonal_(self.decoder.weight)
        self.decoder = self.decoder.to(t_type)

        self.encoder = nn.Sequential(nn.Linear(activation_size, n_dict_components).to(t_type), nn.ReLU())
        self.l1_coef = l1_coef
        self.activation_size = activation_size
        self.n_dict_components = n_dict_components

    def forward(self, x):
        c = self.encoder(x)
        # Apply unit norm constraint to the decoder weights
        self.decoder.weight.data = nn.functional.normalize(self.decoder.weight.data, dim=0)

        x_hat = self.decoder(c)
        return x_hat, c

    @property
    def device(self):
        return next(self.parameters()).device


def cosine_sim(
    vecs1: Union[torch.Tensor, torch.nn.parameter.Parameter, np.ndarray],
    vecs2: Union[torch.Tensor, torch.nn.parameter.Parameter, np.ndarray],
) -> np.ndarray:
    vecs = [vecs1, vecs2]
    for i in range(len(vecs)):
        if not isinstance(vecs[i], np.ndarray):
            vecs[i] = vecs[i].detach().cpu().numpy() # type: ignore
    vecs1, vecs2 = vecs
    normalize = lambda v: (v.T / np.linalg.norm(v, axis=1)).T
    vecs1_norm = normalize(vecs1)
    vecs2_norm = normalize(vecs2)

    return vecs1_norm @ vecs2_norm.T


def mean_max_cosine_similarity(ground_truth_features, learned_dictionary, debug=False):
    # Calculate cosine similarity between all pairs of ground truth and learned features
    cos_sim = cosine_sim(ground_truth_features, learned_dictionary)
    # Find the maximum cosine similarity for each ground truth feature, then average
    mmcs = cos_sim.max(axis=1).mean()
    return mmcs


def get_n_dead_features(auto_encoder, data_generator, n_batches=10, device="cuda"):
    """
    :param result_dict: dictionary containing the results of a single run
    :return: number of dead features

    Estimates the number of dead features in the network by running a few batches of data through the network and
    calculating the mean activation of each feature. If the mean activation is 0 for a feature, it is considered dead.
    """
    t_type = torch.float32
    outputs = []
    for batch_ndx, batch in enumerate(data_generator):
        input = batch[0].to(device).to(t_type)
        with torch.no_grad():
            x_hat, c = auto_encoder(input)
        outputs.append(c)
        if batch_ndx >= n_batches:
            break
    outputs = torch.cat(outputs)  # (n_batches * batch_size, n_dict_components)
    mean_activations = outputs.mean(dim=0)  # (n_dict_components), c is after the ReLU, no need to take abs
    n_dead_features = (mean_activations == 0).sum().item()
    return n_dead_features


def analyse_result(result):
    get_n_dead_features(result)


def run_single_go(cfg: dotdict, data_generator: Optional[RandomDatasetGenerator], mini_run: int = 1, num_mini_runs: int = 1):
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    if not data_generator:
        data_generator = RandomDatasetGenerator(
            activation_dim=cfg.activation_dim,
            n_ground_truth_components=cfg.n_ground_truth_components,
            batch_size=cfg.batch_size,
            feature_num_nonzero=cfg.feature_num_nonzero,
            feature_prob_decay=cfg.feature_prob_decay,
            correlated=cfg.correlated_components,
            device=device,
        )

    t_type = torch.float32
    auto_encoder = AutoEncoder(cfg.activation_dim, cfg.n_components_dictionary, t_type, l1_coef=cfg.l1_alpha).to(device)

    ground_truth_features = data_generator.feats
    # Train the model
    optimizer = optim.Adam(auto_encoder.parameters(), lr=cfg.learning_rate)

    # Hold a running average of the reconstruction loss
    running_recon_loss = 0.0
    time_horizon = 10
    for epoch in range(cfg.epochs):
        epoch_loss = 0.0

        batch = next(data_generator)
        batch = batch + cfg.noise_level * torch.randn_like(batch)

        optimizer.zero_grad()
        # Forward pass
        x_hat, c = auto_encoder(batch)
        # Compute the reconstruction loss and L1 regularization
        l_reconstruction = torch.nn.MSELoss()(batch, x_hat)
        l_l1 = cfg.l1_alpha * torch.norm(c, 1, dim=1).mean()
        # Compute the total loss
        loss = l_reconstruction + l_l1

        # Backward pass
        loss.backward()
        optimizer.step()

        # Add the loss for this batch to the total loss for this epoch
        epoch_loss += loss.item()
        running_recon_loss *= (time_horizon - 1) / time_horizon
        running_recon_loss += l_reconstruction.item() / time_horizon

        if (epoch + 1) % 1000 == 0:
            # Calculate MMCS
            learned_dictionary = auto_encoder.decoder.weight.data.t()
            mmcs = mean_max_cosine_similarity(ground_truth_features.to(auto_encoder.device), learned_dictionary)
            print(f"Mean Max Cosine Similarity: {mmcs:.3f}")

            if True:
                print(f"Epoch {epoch+1}/{cfg.epochs}: Reconstruction = {l_reconstruction:.6f} | l1: {l_l1:.6f}")

    learned_dictionary = auto_encoder.decoder.weight.data.t()
    mmcs = mean_max_cosine_similarity(ground_truth_features.to(auto_encoder.device), learned_dictionary)
    n_dead_features = get_n_dead_features(auto_encoder, data_generator, device=device)
    return mmcs, auto_encoder, n_dead_features, running_recon_loss



def plot_hist(mat, l1_alphas, learned_dict_ratios, show: bool = True, save_folder: str = "", save_name: str = "", title: str = "" ):
    # Create a histogram
    len_alphas, len_dict_ratios = (len(l1_alphas), len(learned_dict_ratios))
    len_dict_ratios -= 1 # Last dict doesn't do anything
    
    fig, axs = plt.subplots(len_dict_ratios, len_alphas, figsize=(7*len_alphas, 5*len_dict_ratios), squeeze=False)

    for i in range(len_dict_ratios):
        max_freq = 0  # To store the maximum frequency in a bin for the current row
        for j in range(len_alphas):
            # Calculate histogram
            counts, bins = np.histogram(mat[j][i], bins=20)
            max_freq = max(max_freq, counts.max())  # Update the maximum frequency if necessary

            # Plot the histogram
            axs[i, j].hist(bins[:-1], bins, weights=counts, edgecolor='black')
            axs[i, j].set_title(f'Dict Ratio: {learned_dict_ratios[i]}| L1 {l1_alphas[j]:.0E}')
            axs[i, j].set_xlim(0,1)
            
        # Set the ylim to the maximum frequency found in the current row
        for ax in axs[i, :]:
            ax.set_ylim(0, max_freq)


    if title:
        fig.suptitle(title, fontsize=20)
    if show:
        plt.show()
    if save_folder:
        plt.savefig(os.path.join(save_folder, save_name))
        plt.close()
        
def plot_mat(mat, l1_alphas, learned_dict_ratios, show: bool = True, save_folder: str = "", save_name: str = "", title: str = "", col_range: Optional[Tuple[float, float]] = None):
    """
    :param mmcs_mat: matrix values
    :param l1_alphas: list of l1_alphas
    :param learned_dict_ratios: list of learned_dict_ratios
    :param show_plots: whether to show the plot
    :param save_path: path to save the plot
    :param title: title of the plot
    :return: None
    """
    assert mat.shape == (len(l1_alphas), len(learned_dict_ratios))
    mat = mat.T
    plt.imshow(mat, interpolation="nearest")
    x_labels = [f"{l1_alpha:.0E}" for l1_alpha in l1_alphas]
    plt.xticks(range(len(x_labels)), x_labels)
    plt.xlabel("l1_alpha")
    y_labels = [str(learned_dict_ratio) for learned_dict_ratio in learned_dict_ratios]
    plt.yticks(range(len(y_labels)), y_labels)
    plt.ylabel("learned_dict_ratio")
    plt.colorbar()
    plt.set_cmap("viridis")
    if col_range:
        # set the colour range
        plt.clim(*col_range)

    plt.xticks(rotation=90)  # turn x labels 90 degrees
    # Add the values in the matrix as text annotations
    for i in range(len(learned_dict_ratios)):
        for j in range(len(l1_alphas)):
            # if type is a float, round to 2 decimal places
            if "float" in str(mat[i, j].dtype):
                plt.text(j, i, format(mat[i, j], ".2E"),
                        ha="center", va="center",
                        color="black" if mat[i, j] > mat.max() / 2 else "w")
            else:
                plt.text(j, i, format(mat[i, j]),
                        ha="center", va="center",
                        color="black" if mat[i, j] > mat.max() / 2 else "w")

    if title:
        plt.title(title)
    if show:
        plt.show()
    if save_folder:
        plt.savefig(os.path.join(save_folder, save_name))
        plt.close()


def compare_mmcs_with_larger_dicts(dict: npt.NDArray, larger_dicts: List[npt.NDArray]) -> float:
    """
    :param dict: The dict to compare to others. Shape (activation_dim, n_dict_elements)
    :param larger_dicts: A list of dicts to compare to. Shape (activation_dim, n_dict_elements(variable)]) * n_larger_dicts
    :return The mean max cosine similarity of the dict to the larger dicts

    Takes a dict, and for each element finds the most similar element in each of the larger dicts, takes the average
    Repeats this for all elements in the dict
    """
    n_larger_dicts = len(larger_dicts)
    n_elements = dict.shape[0]
    max_cosine_similarities = np.zeros((n_elements, n_larger_dicts))
    for elem_ndx in range(n_elements):
        element = np.expand_dims(dict[elem_ndx], 0)
        for dict_ndx, larger_dict in enumerate(larger_dicts):
            cosine_sims = cosine_sim(element, larger_dict).squeeze()
            max_cosine_similarity = max(cosine_sims)
            max_cosine_similarities[elem_ndx, dict_ndx] = max_cosine_similarity
    mean_max_cosine_similarity = max_cosine_similarities.mean()
    return mean_max_cosine_similarity


def recalculate_results(auto_encoder, data_generator):
    """Take a fully trained auto_encoder and a data_generator and return the results of the auto_encoder on the data_generator"""
    time_horizon = 10
    recon_loss = 0
    for epoch in range(time_horizon):
        # Get a batch of data
        batch = data_generator.get_batch()
        batch = torch.from_numpy(batch).to(auto_encoder.device)

        # Forward pass
        x_hat, feat_levels = auto_encoder(batch)

        # Compute the reconstruction loss
        l_reconstruction = torch.norm(x_hat - batch, 2, dim=1).sum() / batch.size(1)

        # Add the loss for this batch to the total loss for this epoch
        recon_loss += l_reconstruction.item() / time_horizon

    ground_truth_features = data_generator.feats
    learned_dictionary = auto_encoder.decoder.weight.data.t()
    mmcs = mean_max_cosine_similarity(ground_truth_features.to(auto_encoder.device), learned_dictionary)
    n_dead_features = get_n_dead_features(auto_encoder, data_generator)
    return mmcs, learned_dictionary, n_dead_features, recon_loss


def run_toy_model(cfg):
    start_time = datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg.model_name = f"toy{cfg.activation_dim}{cfg.learned_dict_ratio}"

    if cfg.use_wandb:
        secrets = json.load(open("secrets.json"))
        wandb.login(key=secrets["wandb_key"])
        wandb_run_name = f"{cfg.model_name}_{start_time[4:]}"  # trim year
        wandb.init(project="sparse coding", config=dict(cfg), name=wandb_run_name, entity="sparse_coding")

    
    # Using a single data generator for all runs so that can compare learned dicts
    data_generator = RandomDatasetGenerator(
        activation_dim=cfg.activation_dim,
        n_ground_truth_components=cfg.n_ground_truth_components,
        batch_size=cfg.batch_size,
        feature_num_nonzero=cfg.feature_num_nonzero,
        feature_prob_decay=cfg.feature_prob_decay,
        correlated=cfg.correlated_components,
        device=cfg.device,
    )

    l1_range = [cfg.l1_exp_base**exp for exp in range(cfg.l1_exp_low, cfg.l1_exp_high)]  # replicate is (-8,9)
    # l1_range = [0.0003] 
    learned_dict_ratios = [cfg.dict_ratio_exp_base**exp for exp in range(cfg.dict_ratio_exp_low, cfg.dict_ratio_exp_high)]  # replicate is (-2,6)
    print("Range of l1 values being used: ", l1_range)
    print("Range of dict_sizes compared to ground truth being used:", learned_dict_ratios)
    mmcs_matrix = np.zeros((len(l1_range), len(learned_dict_ratios)))
    dead_features_matrix = np.zeros((len(l1_range), len(learned_dict_ratios)))
    recon_loss_matrix = np.zeros((len(l1_range), len(learned_dict_ratios)))

    # 2D array of learned dictionaries, indexed by l1_alpha and learned_dict_ratio, start with Nones
    auto_encoders = [[None for _ in range(len(learned_dict_ratios))] for _ in range(len(l1_range))]

    start_time = datetime.now().strftime("%Y%m%d-%H%M%S")

    for l1_alpha, learned_dict_ratio in tqdm(list(itertools.product(l1_range, learned_dict_ratios))):
        cfg.l1_alpha = l1_alpha
        cfg.learned_dict_ratio = learned_dict_ratio
        cfg.n_components_dictionary = int(cfg.n_ground_truth_components * cfg.learned_dict_ratio)
        mmcs, auto_encoder, n_dead_features, reconstruction_loss = run_single_go(cfg, data_generator)
        print(f"l1_alpha: {l1_alpha} | learned_dict_ratio: {learned_dict_ratio} | mmcs: {mmcs:.3f} | n_dead_features: {n_dead_features} | reconstruction_loss: {reconstruction_loss:.3f}")

        mmcs_matrix[l1_range.index(l1_alpha), learned_dict_ratios.index(learned_dict_ratio)] = mmcs
        dead_features_matrix[l1_range.index(l1_alpha), learned_dict_ratios.index(learned_dict_ratio)] = n_dead_features
        recon_loss_matrix[l1_range.index(l1_alpha), learned_dict_ratios.index(learned_dict_ratio)] = reconstruction_loss
        auto_encoders[l1_range.index(l1_alpha)][learned_dict_ratios.index(learned_dict_ratio)] = auto_encoder.cpu()

    outputs_folder = f"outputs"
    outputs_folder = os.path.join(outputs_folder, start_time)
    os.makedirs(outputs_folder, exist_ok=True)

    # Save the matrices and the data generator
    plot_mat(mmcs_matrix, l1_range, learned_dict_ratios, show=False, save_folder=outputs_folder, title="Mean Max Cosine Similarity w/ True", save_name="mmcs_matrix.png", col_range=(0.0, 1.0))
    # clamp dead_features to 0-100 for better visualisation
    # dead_features_matrix = np.clip(dead_features_matrix, 0, 100)
    plot_mat(dead_features_matrix, l1_range, learned_dict_ratios, show=False, save_folder=outputs_folder, title="Dead Neurons", save_name="dead_features_matrix.png")
    plot_mat(recon_loss_matrix, l1_range, learned_dict_ratios, show=False, save_folder=outputs_folder, title="Reconstruction Loss", save_name="recon_loss_matrix.png")
    with open(os.path.join(outputs_folder, "auto_encoders.pkl"), "wb") as f:
        pickle.dump(auto_encoders, f)
    with open(os.path.join(outputs_folder, "config.pkl"), "wb") as f:
        pickle.dump(cfg, f)
    with open(os.path.join(outputs_folder, "data_generator.pkl"), "wb") as f:
        pickle.dump(data_generator, f)
    with open(os.path.join(outputs_folder, "mmcs_matrix.pkl"), "wb") as f:
        pickle.dump(mmcs_matrix, f)
    with open(os.path.join(outputs_folder, "dead_features.pkl"), "wb") as f:
        pickle.dump(dead_features_matrix, f)
    with open(os.path.join(outputs_folder, "recon_loss.pkl"), "wb") as f:
        pickle.dump(recon_loss_matrix, f)

    if(len(learned_dict_ratios) > 1):
        # run MMCS-with-larger at the end of each mini run
        learned_dicts = [[auto_e.decoder.weight.detach().cpu().data.t() for auto_e in l1] for l1 in auto_encoders]
        mmcs_with_larger, feats_above_threshold, full_max_cosine_sim_for_histograms = run_mmcs_with_larger(learned_dicts, threshold=cfg.threshold, device=cfg.device)

        with open(os.path.join(outputs_folder, "larger_dict_compare.pkl"), "wb") as f:
            pickle.dump(mmcs_with_larger, f)
        with open(os.path.join(outputs_folder, "larger_dict_threshold.pkl"), "wb") as f:
            pickle.dump(feats_above_threshold, f)
        with open(os.path.join(outputs_folder, "max_cosine_similarities.pkl"), "wb") as f:
            pickle.dump(full_max_cosine_sim_for_histograms, f)

        plot_mat(mmcs_with_larger, l1_range, learned_dict_ratios, show=False, save_folder=outputs_folder, title="Average MMCS with larger dicts", save_name="av_mmcs_with_larger_dicts.png")
        plot_mat(feats_above_threshold, l1_range, learned_dict_ratios, show=False, save_folder=outputs_folder, title=f"MMCS with larger dicts above {cfg.threshold}", save_name="percentage_above_threshold_mmcs_with_larger_dicts.png")
        plot_hist(full_max_cosine_sim_for_histograms, l1_range, learned_dict_ratios, show=False, save_folder=outputs_folder, title=f"Max Cosine Similarities", save_name="histogram_max_cosine_sim.png")


def run_with_real_data(cfg, auto_encoder: AutoEncoder, completed_batches: int = 0, mini_run: int = 1, n_mini_runs: int = 1):
    optimizer = optim.Adam(auto_encoder.parameters(), lr=cfg.learning_rate)
    running_recon_loss = 0.0
    running_l1_loss = 0.0
    import collections
    feature_activations = np.zeros((cfg.n_components_dictionary))
    running_window = 100
    running_sparsity = collections.deque(maxlen=running_window)
    running_dead_features =collections.deque(maxlen=running_window)
    time_horizon = 1000
    # torch.autograd.set_detect_anomaly(True)
    n_chunks_in_folder = len(os.listdir(cfg.dataset_folder))
<<<<<<< HEAD
    # wb_tag = f"l1={cfg.l1_alpha:.2E}_ds={cfg.n_components_dictionary}"
    wb_tag = ""
=======
    wb_tag = f"l1={cfg.l1_alpha:.2E}_ds={cfg.n_components_dictionary}_mr={mini_run}"
>>>>>>> 7e3713fb71c01566732179e475f8e64827ce72ed
    old_dict = auto_encoder.decoder.weight.detach().cpu().data.t().clone()
    n_batches = 0
    breakout = False
    auto_encoder = auto_encoder.to(cfg.device)
    for epoch in range(cfg.epochs):
        chunk_order = np.random.permutation(n_chunks_in_folder)
        for chunk_ndx, chunk_id in enumerate(chunk_order):
            chunk_loc = os.path.join(cfg.dataset_folder, f"{chunk_id}.pkl")
            dataset = DataLoader(pickle.load(open(chunk_loc, "rb")), batch_size=cfg.batch_size, shuffle=True)
            for batch_idx, batch in enumerate(dataset):
                n_batches += 1
                batch = batch[0].to(cfg.device).to(torch.float32)
                optimizer.zero_grad()
                # Run through auto_encoder

                x_hat, dict_levels = auto_encoder(batch)
                l_reconstruction = torch.nn.MSELoss()(batch, x_hat)
                l_l1 = cfg.l1_alpha * torch.norm(dict_levels, 1, dim=1).mean()
                loss = l_reconstruction + l_l1
                loss.backward()
                optimizer.step()

                # Update running metrics
                sparsity = (dict_levels.detach().count_nonzero(dim=1)).float().mean().item()
                dead_features = (dict_levels.detach().mean(dim=0)==0).count_nonzero().item()
                running_sparsity.append(sparsity)
                running_dead_features.append(dead_features)

                if n_batches == 1:
                    running_recon_loss = l_reconstruction.item()
                    running_l1_loss = l_l1.item()
                    feature_activations = dict_levels.detach().mean(dim=0).cpu().numpy()
                    sparsity = dict_levels.detach().count_nonzero(dim=1).float().mean().item()
                else:
                    running_recon_loss *= (time_horizon - 1) / time_horizon
                    running_recon_loss += l_reconstruction.item() / time_horizon
                    running_l1_loss *= (time_horizon - 1) / time_horizon
                    running_l1_loss += l_l1.item() / time_horizon
                    feature_activations *= (time_horizon - 1) / time_horizon
                    feature_activations += dict_levels.detach().mean(dim=0).cpu().numpy() / time_horizon
                    sparsity *= (time_horizon - 1) / time_horizon
                    sparsity += dict_levels.detach().count_nonzero(dim=1).float().mean().item() / time_horizon

                if (n_batches + completed_batches) % 1000 == 0:
                    new_dict = auto_encoder.decoder.weight.detach().cpu().data.t().clone()
                    feature_angle_shift = check_feature_movement(old_dict, new_dict)
                    old_dict = new_dict

                    momentum_mag = get_size_of_momentum(cfg, optimizer)


                    print(
                        f"L1 Coef: {cfg.l1_alpha:.2E} | Dict ratio: {cfg.n_components_dictionary / cfg.activation_dim} | "
                        + f"Batch: {batch_idx+1}/{len(dataset)} | Chunk: {chunk_ndx+1}/{n_chunks_in_folder} | Minirun: {mini_run + 1}/{n_mini_runs} | "
                        + f"Epoch: {epoch+1}/{cfg.epochs} | Reconstruction loss: {running_recon_loss:.6f} | l1: {l_l1:.6f}"
                    )
                    if cfg.use_wandb:
                        wandb.log(
                            {
                                f"{wb_tag}.reconstruction_loss": running_recon_loss,
                                f"{wb_tag}.l1_loss": l_l1,
                                f"{wb_tag}.feature_angle_shift": feature_angle_shift,
                                f"{wb_tag}.momentum_mag": momentum_mag,
                                f"{wb_tag}.sparsity": sparsity,
                                f"{wb_tag}.dead_features": np.count_nonzero(feature_activations==0),
                                f"total_steps": completed_batches + n_batches,
                            },
                            step=completed_batches + n_batches,
                            commit=True,  # seems to remove weirdness with step numbers
                        )

                if cfg.max_batches and n_batches >= cfg.max_batches:
                    breakout=True
                    break
            if breakout:
                break
        if breakout:
            break
    total_batches = n_batches + completed_batches
    return auto_encoder, running_recon_loss, running_l1_loss, feature_activations, total_batches


def make_activation_dataset(cfg, sentence_dataset: DataLoader, model: HookedTransformer, tensor_name: str, baukit: bool = False) -> pd.DataFrame:
    print(f"Running model and saving activations to {cfg.dataset_folder}")
    with torch.no_grad():
        chunk_size = cfg.chunk_size_gb * (2**30)  # 2GB
        activation_size = cfg.activation_dim * 2 * cfg.model_batch_size * cfg.max_length  # 3072 mlp activations, 2 bytes per half, 1024 context window
        max_chunks = chunk_size // activation_size
        dataset = []
        n_saved_chunks = 0
        for batch_idx, batch in tqdm(enumerate(sentence_dataset)):
            batch = batch["input_ids"].to(cfg.device)
            if baukit:
                # Don't have nanoGPT models integrated with transformer_lens so using baukit for activations
                with Trace(model, tensor_name) as ret:
                    _ = model(batch)
                    mlp_activation_data = ret.output
                    mlp_activation_data = rearrange(mlp_activation_data, "b s n -> (b s) n").to(torch.float16).to(cfg.device)
                    mlp_activation_data = nn.functional.gelu(mlp_activation_data)
            else:
                _, cache = model.run_with_cache(batch)
                mlp_activation_data = cache[tensor_name].to(cfg.device).to(torch.float16)  # NOTE: could do all layers at once, but currently just doing 1 layer
                mlp_activation_data = rearrange(mlp_activation_data, "b s n -> (b s) n")

            dataset.append(mlp_activation_data)
            if len(dataset) >= max_chunks:
                # Need to save, restart the list
                save_activation_chunk(dataset, n_saved_chunks, cfg)
                n_saved_chunks += 1
                print(f"Saved chunk {n_saved_chunks} of activations, total size:  {batch_idx * activation_size} ")
                dataset = []
                if n_saved_chunks == cfg.n_chunks:
                    break
    
        if n_saved_chunks < cfg.n_chunks:
            save_activation_chunk(dataset, n_saved_chunks, cfg)
            print(f"Saved undersized chunk {n_saved_chunks} of activations, total size:  {batch_idx * activation_size} ")

def save_activation_chunk(dataset, n_saved_chunks, cfg):
    dataset_t = torch.cat(dataset, dim=0).to("cpu")
    dataset_obj = torch.utils.data.TensorDataset(dataset_t)
    os.makedirs(cfg.dataset_folder, exist_ok=True)
    with open(cfg.dataset_folder + "/" + str(n_saved_chunks) + ".pkl", "wb") as f:
        pickle.dump(dataset_obj, f)
    

def run_mmcs_with_larger(learned_dicts, threshold=0.9, device: Union[str, torch.device] = "cpu"):
    n_l1_coefs, n_dict_sizes = len(learned_dicts), len(learned_dicts[0])
    av_mmcs_with_larger_dicts = np.zeros((n_l1_coefs, n_dict_sizes))
    feats_above_threshold = np.zeros((n_l1_coefs, n_dict_sizes))
    full_max_cosine_sim_for_histograms = np.empty((n_l1_coefs, n_dict_sizes-1), dtype=object)


    for l1_ndx, dict_size_ndx in tqdm(list(itertools.product(range(n_l1_coefs), range(n_dict_sizes)))):
        if dict_size_ndx == n_dict_sizes - 1:
            continue
        smaller_dict = learned_dicts[l1_ndx][dict_size_ndx]
        # Clone the larger dict, because we're going to zero it out to do replacements
        larger_dict_clone = learned_dicts[l1_ndx][dict_size_ndx + 1].clone().to(device)
        smaller_dict_features, _ = smaller_dict.shape
        larger_dict_features, _ = larger_dict_clone.shape
        # Hungary algorithm
        from scipy.optimize import linear_sum_assignment
        # Calculate all cosine similarities and store in a 2D array
        cos_sims = np.zeros((smaller_dict_features, larger_dict_features))
        for idx, vector in enumerate(smaller_dict):
            cos_sims[idx] = torch.nn.functional.cosine_similarity(vector.to(device), larger_dict_clone, dim=1).cpu().numpy()
        # Convert to a minimization problem
        cos_sims = 1 - cos_sims
        # Use the Hungarian algorithm to solve the assignment problem
        row_ind, col_ind = linear_sum_assignment(cos_sims)
        # Retrieve the max cosine similarities and corresponding indices
        max_cosine_similarities = 1 - cos_sims[row_ind, col_ind]
        av_mmcs_with_larger_dicts[l1_ndx, dict_size_ndx] = max_cosine_similarities.mean().item()
        threshold = 0.9
        feats_above_threshold[l1_ndx, dict_size_ndx] = (max_cosine_similarities > threshold).sum().item() / smaller_dict_features * 100
        full_max_cosine_sim_for_histograms[l1_ndx][dict_size_ndx] = max_cosine_similarities
<<<<<<< HEAD
=======

>>>>>>> 7e3713fb71c01566732179e475f8e64827ce72ed
    return av_mmcs_with_larger_dicts, feats_above_threshold, full_max_cosine_sim_for_histograms


def check_feature_movement(dict: torch.Tensor, old_dict: torch.Tensor):
    """
    Takes in two feature dicts of the same dimension,
    and measures the extent to which they differ.

    """
    assert dict.shape == old_dict.shape
    cos_sims = torch.zeros(dict.shape)
    for i in range(dict.shape[0]):
        cos_sims[i] = torch.nn.functional.cosine_similarity(dict[i], old_dict[i], dim=0)

    total_movement = (1 - cos_sims).mean().item()
    return total_movement

def save_torch_models(models: List[List[AutoEncoder]], path: str) -> None:
    """
    Saves a list of lists of torch models to a given path.
    """
    models_dict = {}
    for l1_models in models:
        for model in l1_models:
            l1_coef = model.l1_coef
            dict_size = model.n_dict_components
            models_dict[f"l1={l1_coef:.2E}_ds={dict_size}"] = model.state_dict()
    
    torch.save(models_dict, path)
            
            
def get_size_of_momentum(cfg: dotdict, optimizer: torch.optim.Optimizer):
    """
    Returns the size of the momentum vector for a given optimizer, for the decoder.
    """
    adam_momentum_tensor = optimizer.state_dict()["state"][0]["exp_avg"]
    decoder_shape = cfg.activation_dim, cfg.n_components_dictionary  # decoder is Linear(n_components, activation_dim) so tensor is stored as (activation_dim, n_components)
    assert adam_momentum_tensor.shape == decoder_shape
    return adam_momentum_tensor.detach().abs().sum().item()  # sum of absolute values of all elements

def setup_data(cfg, tokenizer, model, use_baukit=False, start_line=0):
    sentence_len_lower = 1000
    max_lines = int((cfg.chunk_size_gb * 1e9  * cfg.n_chunks) / (cfg.activation_dim * sentence_len_lower * 2))
    print(f"Setting max_lines to {max_lines} to minimize sentences processed")

    sentence_dataset = make_sentence_dataset(cfg.dataset_name, max_lines=max_lines, start_line=start_line)
    tensor_name = make_tensor_name(cfg.layer, cfg.use_residual, cfg.model_name)
    tokenized_sentence_dataset, bits_per_byte = chunk_and_tokenize(sentence_dataset, tokenizer, max_length=cfg.max_length)
    # breakpoint()
    token_loader = DataLoader(tokenized_sentence_dataset, batch_size=cfg.model_batch_size, shuffle=True)
    make_activation_dataset(cfg, token_loader, model, tensor_name, use_baukit)
    n_lines = len(sentence_dataset)
    return n_lines


def run_real_data_model(cfg: dotdict):
    print(cfg)
    # cfg.model_name = "EleutherAI/pythia-70m-deduped"
    if cfg.model_name in ["gpt2", "EleutherAI/pythia-70m-deduped"]:
        model = HookedTransformer.from_pretrained(cfg.model_name, device=cfg.device)
        use_baukit = False
    elif cfg.model_name == "nanoGPT":
        model_dict = torch.load(open(cfg.model_path, "rb"), map_location="cpu")["model"]
        model_dict = {k.replace("_orig_mod.", ""): v for k, v in model_dict.items()}
        cfg_loc = cfg.model_path[:-3] + "cfg"  # cfg loc is same as model_loc but with .pt replaced with cfg.py
        cfg_loc = cfg_loc.replace("/", ".")
        model_cfg = importlib.import_module(cfg_loc).model_cfg
        model = GPT(model_cfg).to(cfg.device)
        model.load_state_dict(model_dict)
        use_baukit = True
    else:
        raise ValueError("Model name not recognised")

    if hasattr(model, "tokenizer"):
        tokenizer = model.tokenizer
    else:
        print("Using default tokenizer from gpt2")
        tokenizer = GPT2Tokenizer.from_pretrained("gpt2")

    # Check if we have already run this model and got the activations
    dataset_name = cfg.dataset_name.split("/")[-1] + "-" + cfg.model_name + "-" + str(cfg.layer)
    cfg.dataset_folder = os.path.join(cfg.datasets_folder, dataset_name)
    os.makedirs(cfg.dataset_folder, exist_ok=True)

    if len(os.listdir(cfg.dataset_folder)) == 0:
        print(f"Activations in {cfg.dataset_folder} do not exist, creating them")
        n_lines = setup_data(cfg, tokenizer, model, use_baukit=use_baukit)
    else:
        print(f"Activations in {cfg.dataset_folder} already exist, loading them")
        # get activation_dim from first file
        with open(os.path.join(cfg.dataset_folder, "0.pkl"), "rb") as f:
            dataset = pickle.load(f)
        cfg.activation_dim = dataset.tensors[0][0].shape[-1]
        n_lines = cfg.max_lines
        del dataset

    l1_range = [cfg.l1_exp_base**exp for exp in range(cfg.l1_exp_low, cfg.l1_exp_high)]
    dict_ratios = [cfg.dict_ratio_exp_base**exp for exp in range(cfg.dict_ratio_exp_low, cfg.dict_ratio_exp_high)]
    dict_sizes = [int(cfg.activation_dim * ratio) for ratio in dict_ratios]

    print("Range of l1 values being used: ", l1_range)
    print("Range of dict_sizes being used:", dict_sizes)
    dead_features_matrix = np.zeros((len(l1_range), len(dict_sizes)))
    recon_loss_matrix = np.zeros((len(l1_range), len(dict_sizes)))
    l1_loss_matrix = np.zeros((len(l1_range), len(dict_sizes)))
    feature_activations_matrix = [[None for _ in dict_sizes] for _ in l1_range]

    # 2D array of learned dictionaries, indexed by l1_alpha and learned_dict_ratio, start with Nones
    auto_encoders = [[AutoEncoder(cfg.activation_dim, n_feats, l1_coef=l1_ndx).to(cfg.device) for n_feats in dict_sizes] for l1_ndx in l1_range]
    if cfg.load_autoencoders:
        # We check if the sizes match for any of the saved autoencoders, and if so, load them
        loaded_autoencoders = pickle.load(open(cfg.load_autoencoders, "rb"))
        for autoencoder_list in loaded_autoencoders:
            for ae in autoencoder_list:
                if ae.activation_size != cfg.activation_dim:
                    print(f"Mismatch of activation size, expected {cfg.activation_dim} but got {ae.activation_size}")
                    continue
                if ae.l1_coef in l1_range and ae.decoder.weight.shape[1] in dict_sizes:
                    print("Loading autoencoder with l1_coef", ae.l1_coef, "and dict_size", ae.decoder.weight.shape[1])
                    auto_encoders[l1_range.index(ae.l1_coef)][dict_sizes.index(ae.decoder.weight.shape[1])] = ae
                else:
                    print(f"Unable to match autoencoder with l1_coef {ae.l1_coef} and dict_size {ae.decoder.weight.shape[1]}")

    learned_dicts: List[List[Optional[torch.Tensor]]] = [[None for _ in range(len(dict_sizes))] for _ in range(len(l1_range))]

    start_time = datetime.now().strftime("%Y%m%d-%H%M%S")
    outputs_folder_ = os.path.join(cfg.outputs_folder, start_time)
    outputs_folder = outputs_folder_
    os.makedirs(outputs_folder, exist_ok=True)
    if cfg.use_wandb:
        secrets = json.load(open("secrets.json"))
        wandb.login(key=secrets["wandb_key"])
        wandb_run_name = f"{cfg.model_name}_{cfg.layer}_{start_time[4:]}"  # trim year

    step_n = 0
    for mini_run in tqdm(range(cfg.mini_runs)):
<<<<<<< HEAD
        for l1_ndx, dict_size_ndx in tqdm(list(itertools.product(range(len(l1_range)), range(len(dict_sizes))))):
=======
        if cfg.save_after_mini:
            outputs_folder = os.path.join(outputs_folder_, str(mini_run))
            os.makedirs(outputs_folder, exist_ok=True)

        for l1_ndx, dict_size_ndx in list(itertools.product(range(len(l1_range)), range(len(dict_sizes)))):
>>>>>>> 7e3713fb71c01566732179e475f8e64827ce72ed
            l1_loss = l1_range[l1_ndx]
            dict_size = dict_sizes[dict_size_ndx]
            if cfg.use_wandb:
                wandb.init(project="sparse coding", config=dict(cfg), group=wandb_run_name, name=f"l1={l1_loss:.0E}_dict={dict_size}" ,entity="sparse_coding")

            cfg.l1_alpha = l1_loss
            cfg.n_components_dictionary = dict_size
            auto_encoder = auto_encoders[l1_ndx][dict_size_ndx]

            auto_encoder, reconstruction_loss, l1_loss, feature_activations, completed_batches = run_with_real_data(cfg, auto_encoder, completed_batches=step_n, mini_run=mini_run, n_mini_runs=cfg.mini_runs)
            if l1_ndx == (len(l1_range) - 1) and dict_size_ndx == (len(dict_sizes) - 1):
                step_n = completed_batches

            feature_activations_matrix[l1_ndx][dict_size_ndx] = feature_activations
            dead_features_matrix[l1_ndx, dict_size_ndx] = feature_activations.shape[0] - np.count_nonzero(feature_activations)
            recon_loss_matrix[l1_ndx, dict_size_ndx] = reconstruction_loss
            l1_loss_matrix[l1_ndx, dict_size_ndx] = l1_loss
            
            if cfg.use_wandb:
                wandb.finish()
        if cfg.use_wandb:
            wandb.init(project="sparse coding", config=dict(cfg), group=wandb_run_name+"_graphs", name=f"mini_run:{mini_run}", entity="sparse_coding")

        # run MMCS-with-larger at the end of each mini run
        learned_dicts = [[auto_e.decoder.weight.detach().cpu().data.t() for auto_e in l1] for l1 in auto_encoders]
        mmcs_with_larger, feats_above_threshold, mcs = run_mmcs_with_larger(learned_dicts, threshold=cfg.threshold, device=cfg.device)

        # also just report them as variables
        for l1_ndx, dict_size_ndx in list(itertools.product(range(len(l1_range)), range(len(dict_sizes)))):
            l1_coef = l1_range[l1_ndx]
            dict_size = dict_sizes[dict_size_ndx]
            if(cfg.use_wandb):
<<<<<<< HEAD
                # wb_tag = f"l1={l1_coef:.2E}_ds={dict_size}"
                wandb.log({f".n_dead_features": dead_features_matrix[l1_ndx, dict_size_ndx]}, step=step_n, commit=True)
                wandb.log({f".mmcs_with_larger": mmcs_with_larger[l1_ndx, dict_size_ndx]}, step=step_n, commit=True)
                wandb.log({f".feats_above_threshold": feats_above_threshold[l1_ndx, dict_size_ndx]}, step=step_n, commit=True)
=======
                wb_tag = f"l1={l1_coef:.2E}_ds={dict_size}_mr={mini_run}"
                wandb.log({f"{wb_tag}.n_dead_features": dead_features_matrix[l1_ndx, dict_size_ndx]}, step=step_n, commit=True)
                wandb.log({f"{wb_tag}.mmcs_with_larger": mmcs_with_larger[l1_ndx, dict_size_ndx]}, step=step_n, commit=True)
                wandb.log({f"{wb_tag}.feats_above_threshold": feats_above_threshold[l1_ndx, dict_size_ndx]}, step=step_n, commit=True)
>>>>>>> 7e3713fb71c01566732179e475f8e64827ce72ed
                #TODO decide what to do for full_histogram.
        # dead_features_matrix = np.clip(dead_features_matrix, 0, 100)
        
        plot_mat(
            dead_features_matrix,
            l1_range,
            dict_sizes,
            show=False,
            save_folder=outputs_folder,
            title="Dead Neurons",
            save_name="dead_features_matrix.png",
            col_range=(
                0.0,
                100.0,
            ),
        )
        plot_mat(recon_loss_matrix, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title="Reconstruction Loss", save_name="recon_loss_matrix.png")
        plot_mat(l1_loss_matrix, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title="L1 Loss", save_name="l1_loss_matrix.png")
        # upload images to wandb
        if cfg.use_wandb:
            wandb_tag = f"mr={mini_run}"
            wandb.log({f"{wandb_tag}.dead_features": wandb.Image(os.path.join(outputs_folder, "dead_features_matrix.png"))}, commit=True)
            wandb.log({f"{wandb_tag}.recon_loss": wandb.Image(os.path.join(outputs_folder, "recon_loss_matrix.png"))}, commit=True)
            wandb.log({f"{wandb_tag}.l1_loss": wandb.Image(os.path.join(outputs_folder, "l1_loss_matrix.png"))}, commit=True)
        if(len(dict_sizes) > 1) and cfg.use_wandb:
            plot_mat(mmcs_with_larger, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title="Average mmcs with larger dicts", save_name="av_mmcs_with_larger_dicts.png", col_range=(0.0, 1.0))
            plot_mat(feats_above_threshold, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title=f"MN features abouve {cfg.threshold}", save_name="percentage_above_threshold_mmcs_with_larger_dicts.png")
            plot_hist(mcs, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title=f"Max Cosine Similarities", save_name="histogram_max_cosine_sim.png")
<<<<<<< HEAD
            wandb.log({"mmcs_with_larger": wandb.Image(os.path.join(outputs_folder, "av_mmcs_with_larger_dicts.png"))}, commit=True)
            wandb.log({"feats_above_threshold": wandb.Image(os.path.join(outputs_folder, "percentage_above_threshold_mmcs_with_larger_dicts.png"))}, commit=True)
            wandb.log({"mcs_histogram": wandb.Image(os.path.join(outputs_folder, "histogram_max_cosine_sim.png"))}, commit=True)
=======
            if cfg.use_wandb:
                wandb_tag = f"mr={mini_run}"
                wandb.log({f"{wandb_tag}.mmcs_with_larger": wandb.Image(os.path.join(outputs_folder, "av_mmcs_with_larger_dicts.png"))}, commit=True)
                wandb.log({f"{wandb_tag}.feats_above_threshold": wandb.Image(os.path.join(outputs_folder, "percentage_above_threshold_mmcs_with_larger_dicts.png"))}, commit=True)
                wandb.log({f"{wandb_tag}.mcs_histogram": wandb.Image(os.path.join(outputs_folder, "histogram_max_cosine_sim.png"))}, commit=True)
>>>>>>> 7e3713fb71c01566732179e475f8e64827ce72ed

        if cfg.save_after_mini:
            cpu_autoencoders = [[deepcopy(auto_e).to(torch.device("cpu")) for auto_e in l1] for l1 in auto_encoders]
            minirun_folder = os.path.join(outputs_folder, f"minirun{mini_run}")
            os.makedirs(minirun_folder, exist_ok=True)
            encoders_loc = os.path.join(minirun_folder, "autoencoders.pth")
            activations_loc = os.path.join(minirun_folder, "av_activations.pkl")
            save_torch_models(cpu_autoencoders, encoders_loc)
            with open(activations_loc, "wb") as f:
                pickle.dump(feature_activations_matrix, f)
            
            if cfg.upload_to_aws:
                upload_to_aws(encoders_loc)
                upload_to_aws(activations_loc)

        if cfg.use_wandb:
            wandb.finish()

        if cfg.refresh_data:
            print("Remaking dataset")
            os.system(f"rm -rf {cfg.dataset_folder}/*") # delete the old dataset
            n_new_lines = setup_data(cfg, tokenizer, model, use_baukit, start_line=n_lines)
            n_lines += n_new_lines


    # clamp dead_features to 0-100 for better visualisation
    # dead_features_matrix = np.clip(dead_features_matrix, 0, 100)
    plot_mat(dead_features_matrix, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title="Dead Neurons", save_name="dead_features_matrix.png")
    plot_mat(recon_loss_matrix, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title="Reconstruction Loss", save_name="recon_loss_matrix.png")
    cpu_autoencoders = [[auto_e.to(torch.device("cpu")) for auto_e in l1] for l1 in auto_encoders]
    with open(os.path.join(outputs_folder, f"auto_encoders_{cfg.layer}.pkl"), "wb") as f:
        pickle.dump(cpu_autoencoders, f)
    with open(os.path.join(outputs_folder, "config.pkl"), "wb") as f:
        pickle.dump(cfg, f)
    with open(os.path.join(outputs_folder, "dead_features.pkl"), "wb") as f:
        pickle.dump(dead_features_matrix, f)
    with open(os.path.join(outputs_folder, "recon_loss.pkl"), "wb") as f:
        pickle.dump(recon_loss_matrix, f)

    # Compare each learned dictionary to the larger ones
    learned_dicts = [[auto_e.decoder.weight.detach().cpu().data.t() for auto_e in l1] for l1 in auto_encoders]
    mmcs_with_larger, feats_above_threshold, mcs = run_mmcs_with_larger(learned_dicts, threshold=cfg.threshold, device=cfg.device)

    with open(os.path.join(outputs_folder, "larger_dict_compare.pkl"), "wb") as f:
        pickle.dump(mmcs_with_larger, f)
    with open(os.path.join(outputs_folder, "larger_dict_threshold.pkl"), "wb") as f:
        pickle.dump(feats_above_threshold, f)

    if(len(dict_sizes) > 1):
        plot_mat(mmcs_with_larger, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title="Average mmcs with larger dicts", save_name="av_mmcs_with_larger_dicts.png", col_range=(0.0, 1.0))
        plot_mat(feats_above_threshold, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title=f"MMCS with larger dicts above {cfg.threshold}", save_name="percentage_above_threshold_mmcs_with_larger_dicts.png")
        plot_hist(mcs, l1_range, dict_sizes, show=False, save_folder=outputs_folder, title=f"Max Cosine Similarities", save_name="histogram_max_cosine_sim.png")


def main():
    cfg = parse_args()

    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)

    if cfg.run_toy:
        run_toy_model(cfg)
    else:
        run_real_data_model(cfg)


if __name__ == "__main__":
    main()
