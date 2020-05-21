import os
import sys
import time
import glob
import numpy as np
import torch
import argparse
import torch.nn as nn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn

from torch.autograd import Variable
from train import *
from shrinking import ABS
from config import config
import shutil
import functools
print=functools.partial(print,flush=True)
import pickle
from super_model import Network_ImageNet

sys.path.append("../..")
from utils import *

IMAGENET_TRAINING_SET_SIZE = 1231167

parser = argparse.ArgumentParser("ImageNet")
parser.add_argument('--local_rank', type=int, default=None, help='local rank for distributed training')
parser.add_argument('--batch_size', type=int, default=512, help='batch size')
parser.add_argument('--learning_rate', type=float, default=0.2, help='init learning rate')
parser.add_argument('--min_lr', type=float, default=5e-4, help='min learning rate')
parser.add_argument('--momentum', type=float, default=0.9, help='momentum')
parser.add_argument('--weight_decay', type=float, default=4e-5, help='weight decay')
parser.add_argument('--report_freq', type=float, default=30, help='report frequency')
parser.add_argument('--gpu', type=int, default=0, help='gpu device id')
parser.add_argument('--epochs', type=int, default=400, help='num of training epochs')
parser.add_argument('--classes', type=int, default=1000, help='number of classes')
parser.add_argument('--seed', type=int, default=5, help='random seed')
parser.add_argument('--grad_clip', type=float, default=5, help='gradient clipping')
parser.add_argument('--label_smooth', type=float, default=0.1, help='label smoothing')
parser.add_argument('--init_channels', type=int, default=48, help='num of init channels')
parser.add_argument('--train_dir', type=str, default='data/train', help='path to training dataset')
parser.add_argument('--operations_path', type=str, default='shrunk_search_space.pt', help='shrunk search space')
args = parser.parse_args()

per_epoch_iters = IMAGENET_TRAINING_SET_SIZE // args.batch_size

def main():
    if not torch.cuda.is_available():
        print('no gpu device available')
        sys.exit(1)

    num_gpus = torch.cuda.device_count() 
    np.random.seed(args.seed)
    args.gpu = args.local_rank % num_gpus
    torch.cuda.set_device(args.gpu)
    cudnn.benchmark = True
    cudnn.deterministic = True
    torch.manual_seed(args.seed)
    cudnn.enabled=True
    torch.cuda.manual_seed(args.seed)
    group_name = 'search_space_shrinking'
    print('gpu device = %d' % args.gpu)
    print("args = %s", args)

    torch.distributed.init_process_group(backend='nccl', init_method='env://', group_name = group_name)
    args.world_size = torch.distributed.get_world_size()
    args.batch_size = args.batch_size // args.world_size

    criterion_smooth = CrossEntropyLabelSmooth(args.classes, args.label_smooth).cuda()
    total_iters = args.epochs * per_epoch_iters
    # Max shrinking iterations
    iters = config.op_num

    # Prepare data
    train_loader = get_train_dataloader(args.train_dir, args.batch_size, args.local_rank, total_iters)
    train_dataprovider = DataIterator(train_loader)

    operations = []
    for _ in range(config.edges):
        operations.append(list(range(config.op_num)))
    print('operations={}'.format(operations))

    # Prepare model
    base_model = Network_ImageNet().cuda(args.gpu)
    model, seed = get_warmup_model(train_dataprovider, criterion_smooth, operations, per_epoch_iters, args.seed, args)
    print('arch = {}'.format(model.module.architecture()))
    optimizer, scheduler = get_optimizer_schedule(model, args, total_iters)

    start_iter, ops_dim = 0, 0
    checkpoint_tar = config.checkpoint_cache
    if os.path.exists(checkpoint_tar):
        checkpoint = torch.load(checkpoint_tar, map_location={'cuda:0':'cuda:{}'.format(args.local_rank)})
        start_iter = checkpoint['iter'] + 1
        seed = checkpoint['seed']
        operations = checkpoint['operations']
        model.load_state_dict(checkpoint['state_dict'])
        now = time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(time.time()))
        print('{} load checkpoint..., iter = {}, operations={}'.format(now, start_iter, operations))

        # Reset the scheduler
        cur_iters = (config.first_stage_epochs + (start_iter-1) * config.other_stage_epochs) * per_epoch_iters if start_iter > 0 else 0
        for _ in range(cur_iters):
            if scheduler.get_lr()[0] > args.min_lr:
                scheduler.step()

    # Save the base weights for computing angle
    if start_iter == 0  and args.local_rank == 0:
        torch.save(model.module.state_dict(), config.base_net_cache)
        print('save base weights ...')

    for i in range(start_iter, iters):
        print('search space size: {}'.format(get_search_space_size(operations)))
        # ABS finishes when the size of search space is less than the threshold
        if get_search_space_size(operations) <= config.shrinking_finish_threshold:
            # Save the shrunk search space
            pickle.dump(operations, open(args.operations_path, 'wb'))
            break

        per_stage_iters = config.other_stage_epochs * per_epoch_iters if i > 0 else config.first_stage_epochs * per_epoch_iters
        seed = train(train_dataprovider, optimizer, scheduler, model, criterion_smooth, operations, i, per_stage_iters, seed, args)

        if args.local_rank == 0:
            # Search space shrinking
            load(base_model, config.base_net_cache)
            operations = ABS(base_model, model.module, operations, i)
            now = time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(time.time()))
            print('{} |=> iter = {}, operations={}, seed={}'.format(now, i+1, operations, seed))

            save_checkpoint( { 'operations':operations,
                                'iter':i,
                                'state_dict': model.state_dict(),
                                'seed':seed
                             }, config.checkpoint_cache)

            operations = merge_ops(operations)
            ops_dim = len(operations)

        # Synchronize variable cross multiple processes
        ops_dim = broadcast(obj=ops_dim, src=0)
        if not args.local_rank == 0: operations = np.zeros(ops_dim, dtype=np.int)
        operations = broadcast(obj=operations, src=0)
        operations = split_ops(operations)

if __name__ == '__main__':
  main() 