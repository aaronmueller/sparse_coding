import matplotlib
import torch
import matplotlib.pyplot as plt
import numpy as np

import os
import shutil

import itertools

def plot_bottleneck_scores():
    shutil.rmtree("graphs", ignore_errors=True)
    os.mkdir("graphs")

    scores = torch.load("dict_scores_layer_3.pt")

    print(list(scores.keys()))

    colors = ["red", "blue", "green", "orange", "purple", "brown", "pink", "gray", "olive", "cyan"]
    #markers = ["x", "+", "*", "o", "v", "^", "<", ">", "s", "."]
    styles = ["solid", "dashed", "dashdot", "dotted"]

    taus, sizes, task_metrics, corruptions, keys = [], [], [], [], []

    for key, score in scores.items():
        tau, graph, task_metric, corruption = zip(*score)
        taus.append(tau)
        sizes.append([len(g) for g in graph])
        task_metrics.append(task_metric)
        corruptions.append(corruption)
        keys.append(key)
    
    fig, ax = plt.subplots()

    for (style, color), key, x, y in zip(itertools.product(styles, colors), keys, sizes, task_metrics):
        print(key)
        ax.plot(x, y, c=color, linestyle=style, label=key, alpha=0.5)

    ax.set_xlabel("Bottleneck Size")
    ax.set_ylabel("Per-Task Metric")

    ax.legend()

    fig.savefig("graphs/bottleneck_scores.png")

def plot_erasure_scores():
    shutil.rmtree("graphs", ignore_errors=True)
    os.mkdir("graphs")

    leace_score, leace_edit, base_score = torch.load("leace_scores_layer_2.pt")

    scores = torch.load("erasure_scores_layer_2.pt")

    kl_divs = torch.load("kl_div_scores_layer_2.pt")

    colors = ["red", "blue", "green", "orange", "purple", "brown", "pink", "gray", "olive", "cyan"]
    markers = ["x", "+", "*", "o", "v", "^", "<", ">", "s", "."]

    edit_sizes, prediction_ability, kl_div_scores, keys = [], [], [], []
    for key, score in scores.items():
        _, pred, corruption = zip(*score)
        kl_div = [kl_divs[key + "_" + str(i)] for i in range(len(score))]
        edit_sizes.append(corruption)
        prediction_ability.append(pred)
        kl_div_scores.append(kl_div)
        keys.append(key)
    
    edit_sizes.append([leace_edit])
    prediction_ability.append([leace_score])
    kl_div_scores.append([kl_divs["LEACE"]])
    keys.append("LEACE")

    fig, ax = plt.subplots()

    for color, marker, key, x, y in zip(colors, markers, keys, edit_sizes, prediction_ability):
        ax.scatter(x, y, c=color, marker=marker, label=key, alpha=0.5)

    ax.axhline(y=base_score, color="red", linestyle="dashed", label="Base")

    ax.set_xlabel("Mean Edit")
    ax.set_ylabel("Prediction Ability")

    ax.legend()

    plt.savefig("graphs/erasure_by_edit_magnitude.png")

    plt.close(fig)
    del fig, ax

    fig, ax = plt.subplots()

    for color, marker, key, x, y in zip(colors, markers, keys, kl_div_scores, prediction_ability):
        ax.scatter(x, y, c=color, marker=marker, label=key, alpha=0.5)
    
    ax.axhline(y=base_score, color="red", linestyle="dashed", label="Base")

    ax.set_xlabel("KL Divergence")
    ax.set_ylabel("Prediction Ability")

    ax.legend()

    plt.savefig("graphs/erasure_by_kl_div.png")

if __name__ == "__main__":
    #plot_bottleneck_scores()
    plot_erasure_scores()