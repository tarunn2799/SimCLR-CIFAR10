import hydra
from omegaconf import DictConfig
import logging
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import Adam
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader, SubsetRandomSampler
from torchvision.datasets import CIFAR10
from torchvision import transforms
from torchvision.models import resnet18, resnet34
from models import SimCLR
from tqdm import tqdm
from pytorchtools import EarlyStopping
from collections import defaultdict



logger = logging.getLogger(__name__)


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self, name):
        self.name = name
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


class LinModel(nn.Module):
    """Linear wrapper of encoder."""
    def __init__(self, encoder: nn.Module, feature_dim: int, n_classes: int):
        super().__init__()
        self.enc = encoder
        self.feature_dim = feature_dim
        self.n_classes = n_classes
        self.lin = nn.Linear(self.feature_dim, self.n_classes)

    def forward(self, x):
        return self.lin(self.enc(x))


def run_epoch(model, dataloader, epoch, optimizer=None, scheduler=None):
    if optimizer:
        model.train()
    else:
        model.eval()

    loss_meter = AverageMeter('loss')
    acc_meter = AverageMeter('acc')
    loader_bar = tqdm(dataloader)

    for x, y in loader_bar:

        x, y = x.cuda(), y.cuda()
        logits = model(x)
        loss = F.cross_entropy(logits, y)

        if optimizer:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            if scheduler:
                scheduler.step()

        acc = (logits.argmax(dim=1) == y).float().mean()
        loss_meter.update(loss.item(), x.size(0))
        acc_meter.update(acc.item(), x.size(0))
        if optimizer:
            loader_bar.set_description("Train epoch {}, loss: {:.4f}, acc: {:.4f}"
                                       .format(epoch, loss_meter.avg, acc_meter.avg))
        else:
            loader_bar.set_description("Test epoch {}, loss: {:.4f}, acc: {:.4f}"
                                       .format(epoch, loss_meter.avg, acc_meter.avg))

    return loss_meter.avg, acc_meter.avg


def get_lr(step, total_steps, lr_max, lr_min):
    """Compute learning rate according to cosine annealing schedule."""
    return lr_min + (lr_max - lr_min) * 0.5 * (1 + np.cos(step / total_steps * np.pi))

def get_subset(dataset, pct):
    per_class_values = (len(dataset) * pct) /10
    m = defaultdict(list)
    indices = [] 
    for i, (x,y) in enumerate(dataset):
        if y in m.keys():
            if len(m[y]) < per_class_values:
                m[y].append(i)
        else:
            m[y].append(i)
    for k,v in m.items():
        indices.extend(v)
    
    return indices
    
@hydra.main(config_name = 'simclr_config')
def finetune(args: DictConfig) -> None:
    train_transform = transforms.Compose([transforms.RandomResizedCrop(32),
                                          transforms.RandomHorizontalFlip(p=0.5),
                                          transforms.ToTensor()])
    test_transform = transforms.ToTensor()

    data_dir = hydra.utils.to_absolute_path(args.data_dir)
    train_set = CIFAR10(root=data_dir, train=True, transform=train_transform, download=True)
    test_set = CIFAR10(root=data_dir, train=False, transform=test_transform, download=True)
    indices = get_subset(train_set, args.pct)
    train_set = torch.utils.data.Subset(train_set, indices)
    train_loader = DataLoader(train_set, batch_size=args.batch_size, drop_last=True, shuffle = True)
    test_loader = DataLoader(test_set, batch_size=args.batch_size, shuffle=False)

    # Prepare model
    base_encoder = eval(args.backbone)
    pre_model = SimCLR(base_encoder, projection_dim=args.projection_dim).cuda()
    pre_model.load_state_dict(torch.load('simclr_{}_epoch{}.pt'.format(args.backbone, args.load_epoch)))
    model = LinModel(pre_model.enc, feature_dim=pre_model.feature_dim, n_classes=10)
    model = model.cuda()
    print("DATALOADER" , len(train_loader))
    # Fix encoder
    if args.finetune == True:
        model.enc.requires_grad = False
        parameters = [param for param in model.parameters() if param.requires_grad is True]  # trainable parameters.

    else:
        for param in model.enc.parameters():
            param.requires_grad = False

        parameters = [param for param in model.parameters() if param.requires_grad is True]  # trainable parameters.
    # optimizer = Adam(parameters, lr=0.001)

    optimizer = torch.optim.SGD(
        parameters,
        0.2,   # lr = 0.1 * batch_size / 256, see section B.6 and B.7 of SimCLR paper.
        momentum=args.momentum,
        weight_decay=0.,
        nesterov=True)

    # cosine annealing lr
    scheduler = LambdaLR(
        optimizer,
        lr_lambda=lambda step: get_lr(  # pylint: disable=g-long-lambda
            step,
            args.epochs * len(train_loader),
            args.learning_rate,  # lr_lambda computes multiplicative factor
            1e-3))

    optimal_loss, optimal_acc = 1e5, 0.
    early_stopping = EarlyStopping(patience=2, verbose=True)
    
    for epoch in range(1, args.finetune_epochs + 1):
        train_loss, train_acc = run_epoch(model, train_loader, epoch, optimizer, scheduler)
        test_loss, test_acc = run_epoch(model, test_loader, epoch)

        if train_loss < optimal_loss:
            optimal_loss = train_loss
            optimal_acc = test_acc
            logger.info("==> New best results")
            torch.save(model, 'simclr_lin_{}_best.pt'.format(args.backbone))
        
        early_stopping(test_loss, model)
        
        if early_stopping.early_stop:
            print("Early stopping")
            optimal_loss = train_loss
            optimal_acc = test_acc
            logger.info("==> New best results")
            torch.save(model, 'simclr_lin_{}_best.pt'.format(args.backbone))
            break

    logger.info("Best Test Acc: {:.4f}".format(optimal_acc))


if __name__ == '__main__':
    finetune()


