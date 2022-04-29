import torch
import torch.nn as nn
import torch.nn.functional as F
import dgl
import numpy as np
import time
import tqdm
import argparse
from exp_model.gcn import StochasticTwoLayerGCN
from exp_model.sage import SAGE
from exp_model.gat import  GAT
from exp_model.jknet import JKNet
from inference_helper import InferenceHelper, EdgeControlInferenceHelper, AutoInferenceHelper
from dgl.utils import pin_memory_inplace, unpin_memory_inplace, gather_pinned_tensor_rows

import os
from dgl.data.dgl_dataset import DGLDataset
from dgl.data.utils import load_graphs, save_graphs
import dgl.backend as backend


class OtherDataset(DGLDataset):
  raw_dir = '../dataset/'

  def __init__(self, name, force_reload=False,
               verbose=False, transform=None):
    self.dataset_name = name
    if name == 'friendster':
        self.num_classes = 3
    elif name == "orkut":
        self.num_classes = 10
    elif name == "livejournal1":
        self.num_classes = 50
    super(OtherDataset, self).__init__(name=name,
                                            url=None,
                                            raw_dir=OtherDataset.raw_dir,
                                            force_reload=force_reload,
                                            verbose=verbose)

    def process(self):
        row = []
        col = []
        cur_node = 0
        node_mp = {}
        with open(OtherDataset.raw_dir + "com-friendster.ungraph.txt'", 'r') as f:
            for line in f:
                arr = line.split()
                if arr[0] == '#':
                    continue
                src, dst = int(arr[0]), int(arr[1])
                if src not in node_mp:
                    node_mp[src] = cur_node
                    cur_node += 1
                if dst not in node_mp:
                    node_mp[dst] = cur_node
                    cur_node += 1
                row.append(node_mp[src])
                col.append(node_mp[dst])
        row = np.array(row)
        col = np.array(col)
        graph = dgl.graph((row, col))
        graph = dgl.to_bidirected(graph)
        graph = dgl.to_simple(graph)
        self._graph = graph

  def has_cache(self):
    graph_path = os.path.join(OtherDataset.raw_dir, self.dataset_name + '.bin')
    if os.path.exists(graph_path):
      return True
    return False

  def save(self):
    graph_path = os.path.join(self.save_path, 'dgl_graph.bin')
    save_graphs(graph_path, self._graph)

  def load(self):
    print("loading graph")
    graph_path = os.path.join(OtherDataset.raw_dir, self.dataset_name + '.bin')
    graphs, _ = load_graphs(graph_path)
    self._graph = graphs[0]

  def __getitem__(self, idx):
    assert idx == 0, "This dataset only has one graph"
    return self._graph

  def __len__(self):
    return 1

def load_other_dataset(name, dim):
    st = time.time()
    dataset = OtherDataset(name)
    graph = dataset[0]
    features = np.random.rand(graph.number_of_nodes(), dim)
    labels = np.random.randint(0, dataset.num_classes, size=graph.number_of_nodes())
    graph.ndata['train_mask'] = torch.zeros((graph.number_of_nodes(),), dtype=torch.bool)
    graph.ndata['feat'] = backend.tensor(features, dtype=backend.data_type_dict['float32'])
    graph.ndata['label'] = backend.tensor(labels, dtype=backend.data_type_dict['int64'])
    print(dataset[0])
    print(time.time()-st)
    return graph, dataset.num_classes

def load_reddit():
    from dgl.data import RedditDataset
    data = RedditDataset(self_loop=True)
    g = data[0]
    g.ndata['features'] = g.ndata['feat']
    return g, data.num_classes

def load_ogb(name):
    st = time.time()
    from ogb.nodeproppred import DglNodePropPredDataset
    data = DglNodePropPredDataset(name=name)
    splitted_idx = data.get_idx_split()
    graph, labels = data[0]
    # graph = dgl.to_bidirected(graph, True)
    graph = dgl.add_self_loop(graph)
    labels = labels[:, 0]
    graph.ndata['features'] = graph.ndata['feat']
    graph.ndata['label'] = labels
    in_feats = graph.ndata['features'].shape[1]
    num_labels = len(torch.unique(labels[torch.logical_not(torch.isnan(labels))]))
    print(graph)
    print("loading data:", time.time()-st)

    # Find the node IDs in the training, validation, and test set.
    train_nid, val_nid, test_nid = splitted_idx['train'], splitted_idx['valid'], splitted_idx['test']
    train_mask = torch.zeros((graph.number_of_nodes(),), dtype=torch.bool)
    train_mask[train_nid] = True
    val_mask = torch.zeros((graph.number_of_nodes(),), dtype=torch.bool)
    val_mask[val_nid] = True
    test_mask = torch.zeros((graph.number_of_nodes(),), dtype=torch.bool)
    test_mask[test_nid] = True
    graph.ndata['train_mask'] = train_mask
    graph.ndata['val_mask'] = val_mask
    graph.ndata['test_mask'] = test_mask
    return graph, num_labels

def train(args):
    if args.dataset == "reddit":
        dataset = load_reddit()
    elif args.dataset in ("friendster", "orkut", "livejournal1"):
        dataset = load_other_dataset(args.dataset, args.num_hidden)
    else:
        dataset = load_ogb(args.dataset)
    # dataset = load_reddit()
    g : dgl.DGLHeteroGraph = dataset[0]
    train_mask = g.ndata['train_mask']
    feat = g.ndata['feat']
    labels = g.ndata['label']
    num_classes = dataset[1]
    in_feats = feat.shape[1]
    train_nid = torch.nonzero(train_mask, as_tuple=True)[0]
    hidden_feature = args.num_hidden
    sampler = dgl.dataloading.MultiLayerNeighborSampler([10, 25, 50])
    dataloader = dgl.dataloading.NodeDataLoader(
        g, train_nid, sampler,
        batch_size=2000,
        shuffle=True,
        drop_last=False,
        num_workers=4)

    if args.model == "GCN":
        model = StochasticTwoLayerGCN(args.num_layers, in_feats, hidden_feature, num_classes)
    elif args.model == "SAGE":
        model = SAGE(in_feats, hidden_feature, num_classes, args.num_layers, F.relu, 0.5)
    elif args.model == "GAT":
        model = GAT(args.num_layers, in_feats, hidden_feature, num_classes, [args.num_heads for _ in range(args.num_layers)], F.relu, 0.5, 0.5, 0.5, 0.5)
    elif args.model == "JKNET":
        model = JKNet(in_feats, hidden_feature, num_classes, args.num_layers)
    else:
        raise NotImplementedError()

    if args.gpu == -1:
        device = "cpu"
    else:
        device = "cuda:" + str(args.gpu)
    model = model.to(torch.device(device))
    opt = torch.optim.Adam(model.parameters())
    loss_fcn = nn.CrossEntropyLoss()

    for epoch in range(args.num_epochs):
        for input_nodes, output_nodes, blocks in dataloader:
            blocks = [b.to(torch.device(device)) for b in blocks]
            input_features = feat[input_nodes].to(torch.device(device))
            pred = model(blocks, input_features)
            output_labels = labels[output_nodes].to(torch.device(device))
            loss = loss_fcn(pred, output_labels)
            opt.zero_grad()
            loss.backward()
            opt.step()
            break

    with torch.no_grad():
        if args.topdown:
            print(args.num_layers, args.model, "TOP DOWN", args.batch_size, args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            nids = torch.arange(g.number_of_nodes()).to(g.device)
            sampler = dgl.dataloading.MultiLayerFullNeighborSampler(args.num_layers)
            dataloader = dgl.dataloading.NodeDataLoader(
                g, nids, sampler, batch_size=args.batch_size, 
                shuffle=True, drop_last=False, use_uva=False, device=device, num_workers=0)
            pred = torch.zeros(g.number_of_nodes(), model.out_features)
            pin_memory_inplace(feat)
            t = time.time()
            for input_nodes, output_nodes, blocks in dataloader:
                print(blocks)
                input_features = gather_pinned_tensor_rows(feat, input_nodes)
                pred[output_nodes] = model(blocks, input_features).cpu()
                print(time.time()-t)
                t = time.time()
            unpin_memory_inplace(feat)
            cost_time = time.time() - st
            func_score = (torch.argmax(pred, dim=1) == labels.to(device)).float().sum() / len(pred)
            print("TOP DOWN Inference: {}, inference time: {}".format(func_score, cost_time))

        if args.gpufull:
            print(args.num_layers, args.model, "GPU FULL", args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            pred = model.forward_full(g.to(device), feat.to(device))
            cost_time = time.time() - st
            func_score = (torch.argmax(pred, dim=1) == labels.to(device)).float().sum() / len(pred)
            print("GPU Inference: {}, inference time: {}".format(func_score, cost_time))

        elif args.cpufull:
            print(args.num_layers, args.model, "CPU FULL", args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            model.to('cpu')
            pred = model.forward_full(g, feat)
            model.to(device)
            cost_time = time.time() - st
            func_score = (torch.argmax(pred, dim=1) == labels).float().sum() / len(pred)
            print("CPU Inference: {}, inference time: {}".format(func_score, cost_time))

        elif args.auto:
            print(args.num_layers, args.model, "auto", args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            helper = AutoInferenceHelper(model, torch.device(device), use_uva = args.use_uva, debug = args.debug)
            helper_pred = helper.inference(g, feat)
            cost_time = time.time() - st
            helper_score = (torch.argmax(helper_pred, dim=1) == labels).float().sum() / len(helper_pred)
            print("Helper Inference: {}, inference time: {}".format(helper_score, cost_time))

        else:
            if args.gpu == -1:
                print(args.num_layers, args.model, "CPU", args.batch_size, args.dataset, args.num_heads, args.num_hidden)
            else:
                print(args.num_layers, args.model, "GPU", args.batch_size, args.dataset, args.num_heads, args.num_hidden)
            st = time.time()
            pred = model.inference(g, args.batch_size, torch.device(device), feat, args.use_uva)
            cost_time = time.time() - st
            func_score = (torch.argmax(pred, dim=1) == labels).float().sum() / len(pred)
            if args.gpu != -1:
                print("max memory:", torch.cuda.max_memory_allocated() // 1024 ** 2)
            print("Origin Inference: {}, inference time: {}".format(func_score, cost_time))
        print("\n")

if __name__ == '__main__':
    argparser = argparse.ArgumentParser()
    argparser.add_argument('--use-uva', action="store_true")
    argparser.add_argument('--topdown', action="store_true")
    argparser.add_argument('--cpufull', action="store_true")
    argparser.add_argument('--gpufull', action="store_true")
    argparser.add_argument('--gpu', type=int, default=0,
                           help="GPU device ID. Use -1 for CPU training")
    argparser.add_argument('--model', type=str, default='GCN')
    argparser.add_argument('--auto', action="store_true")
    argparser.add_argument('--debug', action="store_true")
    argparser.add_argument('--num-epochs', type=int, default=0)
    argparser.add_argument('--dataset', type=str, default='ogbn-products')
    argparser.add_argument('--num-hidden', type=int, default=128)
    argparser.add_argument('--num-heads', type=int, default=-1)
    argparser.add_argument('--num-layers', type=int, default=2)
    argparser.add_argument('--batch-size', type=int, default=2000)
    args = argparser.parse_args()

    train(args)
