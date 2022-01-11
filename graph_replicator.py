from torch.fx import Graph, Node

from utils import arg_transform


class GraphReplicator(Graph):
    def __init__(self):
        super().__init__()
        self.env = {}

    def insert_node_copy(self, node: Node):
        new_args = arg_transform(self.env, node.args)
        new_node = self.create_node(node.op, node.target, new_args, node.kwargs, node.name)
        self.env[node.name] = new_node
        return new_node

    def insert_input(self, name):
        new_node = self.placeholder(name)
        self.env[name] = new_node

    def insert_inputs(self, names):
        for name in names:
            self.insert_input(name)

    def insert_output(self, nodes):
        nodes = [self.env[node.name] for node in nodes]
        self.output(nodes[0] if len(nodes) == 1 else tuple(nodes))
