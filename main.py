import os
import logging
from setproctitle import setproctitle

import torch
import torch.nn as nn

from dataloading import build_data
from model import make_model
from utils import prepare_batch # temp
from trainer import Trainer


DATA_DIR = './data'
# FIXME: thorough device control
device = torch.device('cpu') #change to cuda if cuda
# TODO: multi-gpu
multi_gpu = True


exp_name = "correct GAN loss"
setproctitle(exp_name)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S', level=logging.INFO)
logger = logging.getLogger(__name__)


if  __name__ == "__main__":

    data = build_data(DATA_DIR, batch_size=64, device=device)
    cptg = make_model(len(data.vocab), len(data.attr), device=device)
    trainer = Trainer(cptg, data, lambda_=1.0)
    print(cptg)

    trainer.train(epoch=1)
    print(exp_name)



