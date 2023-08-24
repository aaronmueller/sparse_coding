from functools import partial
from itertools import product
from typing import List, Tuple, Union, Any, Dict, Literal, Optional, Callable

from datasets import load_dataset
from einops import rearrange
import matplotlib.pyplot as plt
import matplotlib
import numpy as np
from PIL import Image
from sklearn.cluster import KMeans
from sklearn.manifold import TSNE
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchtyping import TensorType

import tqdm

from transformer_lens import HookedTransformer

from autoencoders.learned_dict import LearnedDict
from autoencoders.pca import BatchedPCA

from activation_dataset import setup_data

import standard_metrics

import copy

from test_datasets.ioi import generate_ioi_dataset
from test_datasets.gender import generate_gender_dataset
from test_datasets.winobias import generate_winobias_dataset

from concept_erasure import LeaceFitter, LeaceEraser

from sklearn.metrics import roc_auc_score

from dataclasses import dataclass

import os

class NullspaceProjector:
    def __init__(self, nullspace):
        self.d_activation = nullspace.shape[0]
        self.nullspace = nullspace.detach().clone() / torch.linalg.norm(nullspace)

    def project(self, tensor: TensorType["batch", "d_activation"]) -> TensorType["batch", "d_activation"]:
        return tensor - (torch.einsum("bd,d->b", tensor, self.nullspace)[..., None] * self.nullspace)
    
    @staticmethod
    def class_means(
        activations: TensorType["batch", "d_activation"],
        class_labels: TensorType["batch"],
    ) -> "NullspaceProjector":
        class_means = torch.stack([
            activations[class_labels == i].mean(dim=0)
            for i in range(activations.shape[0])
        ], dim=0)

        class_means_diff = class_means[1] - class_means[0]

        return NullspaceProjector(class_means_diff)

def resample_ablation_hook(
    lens: LearnedDict,
    features_to_ablate: List[int],
    corrupted_codes: Optional[TensorType["batch", "sequence", "n_dict_components"]] = None,
    ablation_type: Literal["ablation", "reconstruction"] = "ablation",
    handicap: Optional[TensorType["batch", "sequence", "d_activation"]] = None,
    ablation_rank: Literal["full", "partial"] = "partial",
    ablation_mask: Optional[TensorType["batch", "sequence"]] = None,
):
    if corrupted_codes is None:
        corrupted_codes_ = None
    else:
        corrupted_codes_ = corrupted_codes.reshape(-1, corrupted_codes.shape[-1])

    activation_dict = {"output": None}
    
    def reconstruction_intervention(tensor, hook=None):
        nonlocal activation_dict
        B, L, D = tensor.shape
        code = lens.encode(tensor.reshape(-1, D))

        if corrupted_codes_ is None:
            code[:, features_to_ablate] = 0.0
        else:
            code[:, features_to_ablate] = corrupted_codes_[:, features_to_ablate]
        
        reconstr = lens.decode(code).reshape(tensor.shape)

        if handicap is not None:
            output = reconstr + handicap
        else:
            output = reconstr

        if ablation_mask is not None:
            output[~ablation_mask] = tensor[~ablation_mask]

        activation_dict["output"] = output.clone()
        return output
    
    def partial_ablation_intervention(tensor, hook=None):
        nonlocal activation_dict
        B, L, D = tensor.shape
        code = lens.encode(tensor.reshape(-1, D))

        ablation_code = torch.zeros_like(code)

        if corrupted_codes_ is None:
            ablation_code[:, features_to_ablate] = -code[:, features_to_ablate]
        else:
            ablation_code[:, features_to_ablate] = corrupted_codes_[:, features_to_ablate] - code[:, features_to_ablate]
        
        ablation = lens.decode(ablation_code).reshape(tensor.shape)

        if handicap is not None:
            output = tensor + ablation + handicap
        else:
            output = tensor + ablation

        if ablation_mask is not None:
            output[~ablation_mask] = tensor[~ablation_mask]

        activation_dict["output"] = output.clone()
        return output

    def full_ablation_intervention(tensor, hook=None):
        nonlocal activation_dict
        B, L, D = tensor.shape
        code = torch.einsum("bd,nd->bn", tensor.reshape(-1,D), lens.get_learned_dict())

        ablation_code = torch.zeros_like(code)

        if corrupted_codes_ is None:
            ablation_code[:, features_to_ablate] = -code[:, features_to_ablate]
        else:
            ablation_code[:, features_to_ablate] = corrupted_codes_[:, features_to_ablate] - code[:, features_to_ablate]

        ablation = torch.einsum("bn,nd->bd", ablation_code, lens.get_learned_dict()).reshape(tensor.shape)
        output = tensor + ablation

        if ablation_mask is not None:
            output[~ablation_mask] = tensor[~ablation_mask]

        activation_dict["output"] = output.clone()
        return tensor + ablation

    ablation_func = None
    if ablation_type == "reconstruction":
        ablation_func = reconstruction_intervention
    elif ablation_type == "ablation" and ablation_rank == "partial":
        ablation_func = partial_ablation_intervention
    elif ablation_type == "ablation" and ablation_rank == "full":
        ablation_func = full_ablation_intervention
    else:
        raise ValueError(f"Unknown ablation type '{ablation_type}' with rank '{ablation_rank}'")
    
    return ablation_func, activation_dict

def resample_ablation(
    model: HookedTransformer,
    lens: LearnedDict,
    location: standard_metrics.Location,
    clean_tokens: TensorType["batch", "sequence"],
    features_to_ablate: List[int],
    corrupted_codes: Optional[TensorType["batch", "sequence", "n_dict_components"]] = None,
    ablation_type: Literal["ablation", "reconstruction"] = "ablation",
    handicap: Optional[TensorType["batch", "sequence", "d_activation"]] = None,
    ablation_rank: Literal["full", "partial"] = "partial",
    ablation_mask: Optional[TensorType["batch", "sequence"]] = None,
    **kwargs,
) -> Tuple[Any, TensorType["batch", "sequence", "d_activation"]]:
    ablation_func, activation_dict = resample_ablation_hook(
        lens,
        features_to_ablate,
        corrupted_codes=corrupted_codes,
        ablation_type=ablation_type,
        handicap=handicap,
        ablation_rank=ablation_rank,
        ablation_mask=ablation_mask,
    )

    logits = model.run_with_hooks(
        clean_tokens,
        fwd_hooks=[(
            standard_metrics.get_model_tensor_name(location),
            ablation_func,
        )],
        **kwargs,
    )

    return logits, activation_dict["output"]

def save_dataset_activations(
    model: HookedTransformer,
    dataset: TensorType["batch", "sequence"],
    location: standard_metrics.Location,
    n_classes: int,
    classes: TensorType["batch"],
    sequence_lengths: Optional[TensorType["batch"]] = None,
    batch_size: int = 32,
    skip_tokens: int = 0,
    filename: str = "activation_data_erasure.pt",
):
    # {filename} is a tuple of (activations, classes, sequence_positions)

    if skip_tokens is None:
        skip_tokens = 0

    if sequence_lengths is None:
        sequence_lengths = torch.tensor([dataset.shape[1]]*dataset.shape[0], dtype=torch.long, device=dataset.device)
    
    max_seq_len = dataset.shape[1]

    saved_activations = []
    saved_class_labels = []
    saved_sequence_lengths = []

    with torch.no_grad():
        for i in tqdm.tqdm(range(0, dataset.shape[0], batch_size)):
            j = min(i+batch_size, dataset.shape[0])
            batch = dataset[i:j]
            batch_lengths = sequence_lengths[i:j]
            batch_classes = classes[i:j]

            logits, activations = model.run_with_cache(
                batch,
                names_filter=lambda name: name == standard_metrics.get_model_tensor_name(location),
                return_type="logits",
                stop_at_layer=location[0] + 1,
            )
            activations = activations[standard_metrics.get_model_tensor_name(location)]

            for k in range(batch.shape[0]):
                class_id = batch_classes[k].item()
                seq_len = batch_lengths[k].item()
                
                activation = activations[k]
                activation[seq_len:] = 0.0

                saved_activations.append(activation)
                saved_class_labels.append(class_id)
                saved_sequence_lengths.append(seq_len)
    
    saved_activations = torch.stack(saved_activations, dim=0)
    saved_class_labels = torch.tensor(saved_class_labels, dtype=torch.long)
    saved_sequence_lengths = torch.tensor(saved_sequence_lengths, dtype=torch.long)

    torch.save((saved_activations, saved_class_labels, saved_sequence_lengths, skip_tokens), filename)

def ce_distance(clean_activation, activation):
    return torch.linalg.norm(clean_activation - activation, dim=-1)

def ablation_mask_from_seq_lengths(
    seq_lengths: TensorType["batch"],
    max_length: int,
) -> TensorType["batch", "sequence"]:
    B = seq_lengths.shape[0]
    mask = torch.zeros((B, max_length), dtype=torch.bool)
    for i in range(B):
        mask[i, :seq_lengths[i]] = True
    return mask

def approx_feature_erasure(
    model: HookedTransformer,
    lens: LearnedDict,
    location: standard_metrics.Location,
    dataset: TensorType["batch", "sequence"],
    class_labels: TensorType["batch"],
    sequence_lengths: TensorType["batch"],
    scoring_function: Callable[[TensorType["batch", "sequence", "vocab_size"], TensorType["batch"], TensorType["batch"]], TensorType["batch"]],
    directions_filter: Optional[List[int]] = None,
    ablation_type: Literal["ablation", "reconstruction"] = "ablation",
    ablation_rank: Literal["full", "partial"] = "partial",
    test_batch_size: int = 32,
) -> List[Tuple[int, float]]:
    """Try ablations with directions and see which ones are best"""
    if directions_filter is None:
        directions_filter = list(range(lens.get_learned_dict().shape[0]))
    
    scores = []

    for i in tqdm.tqdm(directions_filter):
        batch_idxs = np.random.choice(dataset.shape[0], size=test_batch_size, replace=False)
        batch = dataset[batch_idxs]
        batch_classes = class_labels[batch_idxs]
        batch_lengths = sequence_lengths[batch_idxs]

        batch_logits, _ = resample_ablation(
            model,
            lens,
            location,
            batch,
            [i],
            ablation_type=ablation_type,
            ablation_rank=ablation_rank,
            return_type="logits",
        )

        score = scoring_function(batch_logits, batch_classes, batch_lengths).mean().item()
        scores.append((i, score))
    
    return sorted(scores, key=lambda x: x[1])

def filter_activation_threshold(
    lens: LearnedDict,
    dataset: TensorType["batch", "sequence", "d_activation"],
    sequence_lengths: TensorType["batch"],
    activation_proportion_threshold: float = 0.05,
    batch_size: int = 32,
    last_position_only: bool = False,
) -> List[int]:
    if last_position_only:
        zero_mask = torch.zeros((dataset.shape[0], dataset.shape[1]), dtype=torch.bool)
        zero_mask[torch.arange(sequence_lengths.shape[0]), sequence_lengths-1] = True
    else:    
        zero_mask = ablation_mask_from_seq_lengths(sequence_lengths, dataset.shape[1])


    feat_activation_count = torch.zeros(lens.get_learned_dict().shape[0], dtype=torch.long, device=dataset.device)
    total_activations = 0

    for i in tqdm.tqdm(range(0, dataset.shape[0], batch_size)):
        j = min(i+batch_size, dataset.shape[0])
        batch = dataset[i:j]

        encoded_batch = lens.encode(batch.reshape(-1, batch.shape[-1])).reshape(batch.shape[0], batch.shape[1], -1)
        batch_nz = (encoded_batch != 0.0).long()
        
        batch_nz[~zero_mask[i:j]] = 0

        feat_activation_count += batch_nz.sum(dim=(0, 1))

        if last_position_only:
            total_activations += batch.shape[0]
        else:
            total_activations += sequence_lengths[i:j].sum().item()
    
    feat_activation_proportions = feat_activation_count.float() / total_activations

    return torch.where(feat_activation_proportions > activation_proportion_threshold)[0].tolist()

def eval_hook(
    model: HookedTransformer,
    hook_func: Callable[[TensorType["batch", "sequence", "d_activation"], TensorType["batch"], TensorType["batch"], Any], TensorType["batch", "sequence", "d_activation"]],
    dataset: TensorType["batch", "sequence"],
    class_labels: TensorType["batch"],
    sequence_lengths: TensorType["batch"],
    location: standard_metrics.Location,
    task_score_func: Callable[[TensorType["batch", "sequence", "vocab_size"], TensorType["batch"], TensorType["batch"]], TensorType["batch"]],
    activation_dist_func: Callable[[TensorType["batch", "d_activation"], TensorType["batch", "d_activation"]], TensorType["batch"]] = ce_distance,
    batch_size: int = 4,
    last_position_only: bool = False,
) -> Tuple[float, float, float]:
    # returns (task_score, activation_dist)

    model.eval()

    mean_activation_dist = 0.0
    mean_task_score = 0.0

    for i in range(0, dataset.shape[0], batch_size):
        j = min(i+batch_size, dataset.shape[0])
        batch = dataset[i:j]
        batch_lengths = sequence_lengths[i:j]
        batch_classes = class_labels[i:j]

        activation_dist = None

        def hook_func_wrapper(tensor, hook=None):
            nonlocal activation_dist
            _, L, D = tensor.shape
            uneditied = tensor.clone()
            if last_position_only:
                edited = tensor.clone()
                edited[torch.arange(batch_lengths.shape[0]), batch_lengths-1] = hook_func(
                    tensor[torch.arange(batch_lengths.shape[0]), batch_lengths-1],
                    batch_classes,
                    batch_lengths,
                    hook=hook
                )
                activation_dist = activation_dist_func(
                    uneditied[torch.arange(batch_lengths.shape[0]), batch_lengths-1],
                    edited[torch.arange(batch_lengths.shape[0]), batch_lengths-1],
                )
            else:
                edited = hook_func(tensor, batch_classes, batch_lengths, hook=hook)
                activation_dist = activation_dist_func(uneditied.reshape(-1, L*D), edited.reshape(-1, L*D))
            
            return edited

        logits = model.run_with_hooks(
            batch,
            fwd_hooks=[(
                standard_metrics.get_model_tensor_name(location),
                hook_func_wrapper,
            )],
            return_type="logits",
        )

        mean_task_score += task_score_func(logits, batch_classes, batch_lengths).sum().item()

        mean_activation_dist += activation_dist.sum().item()
    
    mean_activation_dist /= dataset.shape[0]
    mean_task_score /= dataset.shape[0]

    return mean_task_score, mean_activation_dist

def generate_activation_data(cfg):
    model_name = cfg.model_name
    device = cfg.device

    model = HookedTransformer.from_pretrained(model_name)
    model.to(device)
    model.eval()
    model.requires_grad_(False)

    prompts, classes, class_tokens, sequence_lengths, skip_tokens = generate_gender_dataset(
        model_name,
        model.tokenizer.pad_token_id,
        count_cutoff=cfg.count_cutoff,
        sample_n=250,
        randomise=False,    
    )
    prompts = prompts.to(device)
    classes = classes.to(device)
    sequence_lengths = sequence_lengths.to(device)
    
    save_dataset_activations(
        model,
        prompts,
        (cfg.layer, "residual"),
        2,
        classes,
        sequence_lengths=sequence_lengths,
        batch_size=32,
        skip_tokens=skip_tokens,
        filename=cfg.activation_filename
    )

def gen_pca_simplification(cfg):
    device = cfg.device

    activations, class_labels, sequence_lengths, skip_tokens = torch.load(cfg.activation_filename)

    B, L, D = activations.shape

    activations = activations[:, skip_tokens:]

    pca_components = torch.empty((L-skip_tokens, 2, D), dtype=torch.float, device=device)

    optimal_activations = torch.empty((B, L-skip_tokens, D), dtype=torch.float, device=device)
    optimal_activations_proj = torch.empty((B, L-skip_tokens, 2), dtype=torch.float, device=device)

    for i in tqdm.tqdm(range(L-skip_tokens)):
        u, s, v = torch.linalg.svd(activations[:, i])
        pca_components[i] = v[:2]
        optimal_eraser = LeaceEraser.fit(
            activations[:, i],
            class_labels,
        )
        optimal_activations[:, i] = optimal_eraser(activations[:, i])
    
    optimal_activations_proj = torch.einsum("bld,lnd->bln", optimal_activations, pca_components)

    projected_activations = torch.einsum("bld,lnd->bln", activations, pca_components)
    #projected_activations = projected_activations.reshape(B, L-skip_tokens, 2)

    leace_eraser = torch.load(f"{cfg.output_folder}/leace_eraser_layer_{cfg.layer}.pt")

    erased_activations = leace_eraser(activations)
    erased_activations_proj = torch.einsum("bld,lnd->bln", erased_activations, pca_components)
    #erased_activations = erased_activations.reshape(B, L-skip_tokens, 2)

    from contextlib import nullcontext

    import matplotlib.pyplot as plt
    import matplotlib

    # have a color for every combination of class and sequence length

    male_projected = projected_activations[class_labels == 0].detach().cpu().numpy()
    male_erased = erased_activations_proj[class_labels == 0].detach().cpu().numpy()
    male_optimal = optimal_activations_proj[class_labels == 0].detach().cpu().numpy()

    female_projected = projected_activations[class_labels == 1].detach().cpu().numpy()
    female_erased = erased_activations_proj[class_labels == 1].detach().cpu().numpy()
    female_optimal = optimal_activations_proj[class_labels == 1].detach().cpu().numpy()

    male_cmap = matplotlib.cm.get_cmap("Blues")
    female_cmap = matplotlib.cm.get_cmap("Reds")

    token_positions = np.arange(L-skip_tokens)
    hues = np.linspace(0.3, 0.7, L-skip_tokens)

    os.makedirs(f"{cfg.output_folder}/pca_img", exist_ok=True)

    for t in token_positions:
        fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(30, 10))

        ax1.scatter(male_projected[:, t, 0], male_projected[:, t, 1], color=male_cmap(hues[t]))
        ax2.scatter(male_erased[:, t, 0], male_erased[:, t, 1], color=male_cmap(hues[t]))
        ax3.scatter(male_optimal[:, t, 0], male_optimal[:, t, 1], color=male_cmap(hues[t]))

        ax1.scatter(female_projected[:, t, 0], female_projected[:, t, 1], color=female_cmap(hues[t]))
        ax2.scatter(female_erased[:, t, 0], female_erased[:, t, 1], color=female_cmap(hues[t]))
        ax3.scatter(female_optimal[:, t, 0], female_optimal[:, t, 1], color=female_cmap(hues[t]))

        ax1.set_title("Original")
        ax2.set_title("Erased")
        ax3.set_title("Optimal")

        plt.savefig(f"{cfg.output_folder}/pca_img/pca_simplification_layer_{cfg.layer}_pos_{t}.png")

        plt.close()

    overall_pca = torch.empty((2, D), dtype=torch.float, device=device)

    with nullcontext():
        u, s, v = torch.linalg.svd(activations.reshape(-1, D))
        overall_pca = v[:2]
    
    optimal_activations_proj = torch.einsum("bld,nd->bln", optimal_activations, overall_pca)
    erased_activations_proj = torch.einsum("bld,nd->bln", erased_activations, overall_pca)
    projected_activations = torch.einsum("bld,nd->bln", activations, overall_pca)

    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(30, 10))

    male_projected = projected_activations[class_labels == 0].detach().cpu().numpy()
    male_erased = erased_activations_proj[class_labels == 0].detach().cpu().numpy()
    male_optimal = optimal_activations_proj[class_labels == 0].detach().cpu().numpy()

    female_projected = projected_activations[class_labels == 1].detach().cpu().numpy()
    female_erased = erased_activations_proj[class_labels == 1].detach().cpu().numpy()
    female_optimal = optimal_activations_proj[class_labels == 1].detach().cpu().numpy()

    for t in token_positions:
        ax1.scatter(male_projected[:, t, 0], male_projected[:, t, 1], color=male_cmap(hues[t]))
        ax2.scatter(male_erased[:, t, 0], male_erased[:, t, 1], color=male_cmap(hues[t]))
        ax3.scatter(male_optimal[:, t, 0], male_optimal[:, t, 1], color=male_cmap(hues[t]))

        ax1.scatter(female_projected[:, t, 0], female_projected[:, t, 1], color=female_cmap(hues[t]))
        ax2.scatter(female_erased[:, t, 0], female_erased[:, t, 1], color=female_cmap(hues[t]))
        ax3.scatter(female_optimal[:, t, 0], female_optimal[:, t, 1], color=female_cmap(hues[t]))

    ax1.set_title("Original")
    ax2.set_title("Erased")
    ax3.set_title("Optimal")

    plt.savefig(f"{cfg.output_folder}/pca_img/pca_simplification_layer_{cfg.layer}_overall.png")

    plt.close()

def fit_leace_eraser(cfg):
    device = cfg.device

    activations, class_labels, sequence_lengths, skip_tokens = torch.load(cfg.activation_filename)
    
    B, L, D = activations.shape

    if cfg.last_position_only:
        eraser = LeaceEraser.fit(
            activations[torch.arange(sequence_lengths.shape[0]), sequence_lengths-1],
            class_labels,
        )
    else:
        mask = ablation_mask_from_seq_lengths(sequence_lengths, L-skip_tokens)

        activations = activations[:, skip_tokens:][mask]
        class_labels = class_labels.unsqueeze(1).expand(-1, L-skip_tokens)[mask]

        eraser = LeaceEraser.fit(
            activations,
            class_labels,
        )

    torch.save(eraser, f"{cfg.output_folder}/leace_eraser_layer_{cfg.layer}.pt")

def fit_means_eraser(cfg):
    device = cfg.device

    activations, class_labels, sequence_lengths, skip_tokens = torch.load(cfg.activation_filename)
    
    B, L, D = activations.shape

    if cfg.last_position_only:
        projector = NullspaceProjector.class_means(
            activations[torch.arange(sequence_lengths.shape[0]), sequence_lengths-1],
            class_labels,
        )
    else:
        mask = ablation_mask_from_seq_lengths(sequence_lengths, L-skip_tokens)

        activations = activations[:, skip_tokens:][mask]
        class_labels = class_labels.unsqueeze(1).expand(-1, L-skip_tokens)[mask]

        projector = NullspaceProjector.class_means(
            activations,
            class_labels,
        )

    torch.save(projector, f"{cfg.output_folder}/means_eraser_layer_{cfg.layer}.pt")

def gender_prediction(class_tokens):
    def go(logits, class_labels, sequence_lengths):
        preds = logits[torch.arange(sequence_lengths.shape[0]), sequence_lengths-1]
        preds = F.softmax(preds[:, [class_tokens[0], class_tokens[1]]], dim=-1)
        labels_one_hot = F.one_hot(class_labels, num_classes=2).float()
        return torch.einsum("bc,bc->b",preds,labels_one_hot)
    return go

def rank_dict_features_expensive(cfg):
    model_name = cfg.model_name
    device = cfg.device

    activations, _, sequence_lengths, _ = torch.load(cfg.activation_filename)

    dicts = torch.load(cfg.dict_filename.format(layer=cfg.layer))

    target_l1 = cfg.target_l1
    target_dict_size = cfg.dict_size
    best_dist = None
    best_dict = None

    for dict, hyperparams in dicts:
        if hyperparams["dict_size"] == target_dict_size:
            dist = abs(hyperparams["l1_alpha"] - target_l1)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_dict = dict
    
    best_dict.to_device(device)

    filtered_idxs = filter_activation_threshold(
        best_dict,
        activations,
        sequence_lengths,
        batch_size=32,
        activation_proportion_threshold=cfg.feature_freq_threshold,
        last_position_only=cfg.last_position_only,
    )

    del activations, sequence_lengths

    prompts, class_labels, class_tokens, sequence_lengths, _ = generate_gender_dataset(
        model_name,
        count_cutoff=cfg.count_cutoff,
        sample_n=32,
    )

    prompts = prompts.to(device)
    class_labels = class_labels.to(device)
    sequence_lengths = sequence_lengths.to(device)

    model = HookedTransformer.from_pretrained(model_name)
    model.to(device)
    model.eval()
    model.requires_grad_(False)
    
    scores = []

    base_logits = model(prompts, return_type="logits")

    for idx in tqdm.tqdm(filtered_idxs):
        feature = best_dict.get_learned_dict()[idx].to(device)

        projector = NullspaceProjector(feature)

        def hook(tensor, class_labels, seq_lengths, hook=None):
            if cfg.last_position_only:
                return projector.project(tensor)
            else:
                B, L, D = tensor.shape
                return projector.project(tensor.reshape(-1, D)).reshape(tensor.shape)

        task_score, _ = eval_hook(
            model,
            hook,
            prompts,
            class_labels,
            sequence_lengths,
            (cfg.layer, "residual"),
            task_score_func=gender_prediction(class_tokens),
            batch_size=32,
            last_position_only=cfg.last_position_only,
        )

        scores.append((idx, task_score))

    scores = sorted(scores, key=lambda x: x[1])

    torch.save(scores, f"{cfg.output_folder}/dict_feature_scores_layer_{cfg.layer}.pt")
    torch.save(best_dict, f"{cfg.output_folder}/best_dict_layer_{cfg.layer}.pt")

TEST_N_SCORES = 10

def evaluate_interventions(cfg):
    device = cfg.device
    model_name = cfg.model_name
    layer = cfg.layer
    
    model = HookedTransformer.from_pretrained(model_name)
    model.to(device)
    model.eval()
    model.requires_grad_(False)

    prompts, class_labels, class_tokens, sequence_lengths, _ = generate_gender_dataset(
        model_name,
        count_cutoff=10000,
        sample_n=250,
        randomise=False,
    )
    prompts = prompts.to(device)
    class_labels = class_labels.to(device)
    sequence_lengths = sequence_lengths.to(device)
    
    best_dict = torch.load(f"{cfg.output_folder}/best_dict_layer_{cfg.layer}.pt")
    best_dict.to_device(device)
    best_dict_scores = torch.load(f"{cfg.output_folder}/dict_feature_scores_layer_{cfg.layer}.pt")
    best_dict_scores = best_dict_scores[:cfg.test_n_scores]

    dict_scores = []

    base_logits = model(prompts, return_type="logits")

    base_score = gender_prediction(class_tokens)(base_logits, class_labels, sequence_lengths).mean().item()
    print(f"base score: {base_score}")

    for feat_idx, sep in best_dict_scores:
        feature = best_dict.get_learned_dict()[feat_idx].to(device)

        projector = NullspaceProjector(feature)

        def hook(tensor, class_labels, seq_lengths, hook=None):
            if cfg.last_position_only:
                return projector.project(tensor)
            else:
                B, L, D = tensor.shape
                return projector.project(tensor.reshape(-1, D)).reshape(tensor.shape)
        
        task_score, activation_dist = eval_hook(
            model,
            hook,
            prompts,
            class_labels,
            sequence_lengths,
            (layer, "residual"),
            task_score_func=gender_prediction(class_tokens),
            batch_size=32,
            last_position_only=cfg.last_position_only,
        )

        dict_scores.append((feat_idx, task_score, activation_dist))
        print(f"feat: {feat_idx}, score: {task_score}, dist: {activation_dist}")
    
    leace_eraser = torch.load(f"{cfg.output_folder}/leace_eraser_layer_{cfg.layer}.pt")

    def leace_hook(tensor, class_labels, seq_lengths, hook=None):
        if cfg.last_position_only:
            return leace_eraser(tensor)
        else:
            B, L, D = tensor.shape
            return leace_eraser(tensor.reshape(-1, D)).reshape(tensor.shape)

    leace_score, leace_dist = eval_hook(
        model,
        leace_hook,
        prompts,
        class_labels,
        sequence_lengths,
        (layer, "residual"),
        task_score_func=gender_prediction(class_tokens),
        batch_size=32,
        last_position_only=cfg.last_position_only,
    )

    print(f"leace score: {leace_score}, dist: {leace_dist}")

    means_eraser = torch.load(f"{cfg.output_folder}/means_eraser_layer_{cfg.layer}.pt")

    def means_hook(tensor, class_labels, seq_lengths, hook=None):
        if cfg.last_position_only:
            return means_eraser.project(tensor)
        else:
            B, L, D = tensor.shape
            return means_eraser.project(tensor.reshape(-1, D)).reshape(tensor.shape)

    means_score, means_dist = eval_hook(
        model,
        means_hook,
        prompts,
        class_labels,
        sequence_lengths,
        (layer, "residual"),
        task_score_func=gender_prediction(class_tokens),
        batch_size=32,
        last_position_only=cfg.last_position_only,
    )

    print(f"means score: {means_score}, dist: {means_dist}")

    torch.save({
        "leace": (leace_score, leace_dist),
        "means": (means_score, means_dist),
        "dict": dict_scores,
        "base": base_score},
    f"{cfg.output_folder}/eval_layer_{cfg.layer}.pt")

def eval_on_winobias(cfg):
    device = cfg.device
    model_name = cfg.model_name
    layer = cfg.layer
    
    model = HookedTransformer.from_pretrained(model_name)
    model.to(device)
    model.eval()
    model.requires_grad_(False)

    prompts, class_labels, class_tokens, sequence_lengths = generate_winobias_dataset(
        model_name,
    )
    prompts = prompts.to(device)
    class_labels = class_labels.to(device)
    sequence_lengths = sequence_lengths.to(device)

    best_dict = torch.load(f"{cfg.output_folder}/best_dict_layer_{cfg.layer}.pt")
    best_dict.to_device(device)
    best_dict_scores = torch.load(f"{cfg.output_folder}/dict_feature_scores_layer_{cfg.layer}.pt")
    best_dict_scores = best_dict_scores[:cfg.test_n_scores]

    dict_scores = []

    base_logits = model(prompts, return_type="logits")

    base_score = gender_prediction(class_tokens)(base_logits, class_labels, sequence_lengths).mean().item()
    print(f"base score: {base_score}")

    for feat_idx, sep in best_dict_scores:
        feature = best_dict.get_learned_dict()[feat_idx].to(device)

        def hook(tensor, hook=None):
            B, L, D = tensor.shape
            return tensor - (torch.einsum("bd,d->b", tensor.reshape(B*L, D), feature).unsqueeze(1) @ feature.unsqueeze(0)).reshape(tensor.shape)
        
        task_score, activation_dist = eval_hook(
            model,
            hook,
            prompts,
            class_labels,
            sequence_lengths,
            (layer, "residual"),
            task_score_func=gender_prediction(class_tokens),
            batch_size=32,
        )

        dict_scores.append((feat_idx, task_score, activation_dist))
        print(f"feat: {feat_idx}, score: {task_score}, dist: {activation_dist}")
    
    means_eraser = torch.load(f"{cfg.output_folder}/means_eraser_layer_{cfg.layer}.pt")

    def means_hook(tensor, hook=None):
        B, L, D = tensor.shape
        return means_eraser.project(tensor.reshape(B*L, D)).reshape(tensor.shape)
    
    means_score, means_dist = eval_hook(
        model,
        means_hook,
        prompts,
        class_labels,
        sequence_lengths,
        (layer, "residual"),
        task_score_func=gender_prediction(class_tokens),
        batch_size=32,
    )

    print(f"means score: {means_score}, dist: {means_dist}")

    torch.save({
        "dict": dict_scores,
        "base": base_score,
        "means": (means_score, means_dist),
    }, f"{cfg.output_folder}/eval_winobias_layer_{cfg.layer}.pt")

def gender_prediction_everything():
    from utils import dotdict

    cfg = dotdict({
        "model_name": "EleutherAI/pythia-70m-deduped",
        "device": "cuda:4",
        "layer": None,
        "count_cutoff": 10000,
        "output_folder": "output_erasure",
        "activation_filename": "activation_data_erasure.pt",
        "dict_filename": "/mnt/ssd-cluster/bigrun0308/tied_residual_l{layer}_r4/_9/learned_dicts.pt",
        "target_l1": 8e-4,
        "dict_size": 2048,
        "feature_freq_threshold": 0.05,
        "test_n_scores": 10,
        "last_position_only": False,
    })

    layers = [0, 1, 2, 3, 4, 5]

    os.makedirs(cfg.output_folder, exist_ok=True)

    for layer in layers:
        cfg.layer = layer
        generate_activation_data(cfg)
        fit_leace_eraser(cfg)
        fit_means_eraser(cfg)
        rank_dict_features_expensive(cfg)
        evaluate_interventions(cfg)

def winobias_prediction_everything():
    from utils import dotdict

    cfg = dotdict({
        "model_name": "EleutherAI/pythia-70m-deduped",
        "device": "cuda:4",
        "layer": None,
        "count_cutoff": 10000,
        "output_folder": "output_erasure_pca",
        "activation_filename": "activation_data_erasure.pt",
        "dict_filename": "/mnt/ssd-cluster/bigrun0308/tied_residual_l{layer}_r4/_9/learned_dicts.pt",
        "target_l1": 8e-4,
        "dict_size": 2048,
        "feature_freq_threshold": 0.05,
        "test_n_scores": 10,
    })

    layers = [0, 1, 2, 3, 4, 5]

    os.makedirs(cfg.output_folder, exist_ok=True)

    for layer in layers:
        cfg.layer = layer
        eval_on_winobias(cfg)

if __name__ == "__main__":
    from sys import argv

    if argv[1] == "gender":
        gender_prediction_everything()
    elif argv[1] == "winobias":
        winobias_prediction_everything()
    elif argv[1] == "pca":
        from utils import dotdict

        cfg = dotdict({
            "model_name": "EleutherAI/pythia-70m-deduped",
            "device": "cuda:4",
            "layer": 5,
            "count_cutoff": 10000,
            "output_folder": "output_erasure_pca",
            "activation_filename": "activation_data_erasure.pt",
            "dict_filename": "/mnt/ssd-cluster/bigrun0308/tied_residual_l{layer}_r4/_9/learned_dicts.pt",
            "target_l1": 8e-4,
            "dict_size": 2048,
            "feature_freq_threshold": 0.05,
            "test_n_scores": 10,
            "last_position_only": False,
        })

        generate_activation_data(cfg)
        fit_leace_eraser(cfg)
        gen_pca_simplification(cfg)