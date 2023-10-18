import torch
import json
from dataset import CharDataset
from datasets import Dataset
from othello_data import get as get_othello

othello = get_othello(ood_num=-1, data_root=None, wthor=True)
char_dataset = CharDataset(othello)

with open("data/othello_hf.json", "w") as json_data:
    for item in char_dataset:
        text = item[0].tolist()
        text.append(item[1].tolist()[-1])
        data = json.dumps({"text": text})
        json_data.write(data+"\n")
