# coding=utf-8
import os
import re
import sys
from my_lib.neural_module.learn_strategy import LrWarmUp
from my_lib.neural_module.transformer import TranEnc, TranDec, DualTranDec,ResFF,ResMHA
from my_lib.neural_module.embedding import PosEnc
from my_lib.neural_module.loss import LabelSmoothSoftmaxCEV2, CriterionNet
from my_lib.neural_module.balanced_data_parallel import BalancedDataParallel
from my_lib.neural_module.copy_attention import DualMultiCopyGenerator,MultiCopyGenerator,DualCopyGenerator
from my_lib.neural_module.beam_search import trans_beam_search
from my_lib.neural_model.seq_to_seq_model import TransSeq2Seq
from my_lib.neural_model.base_model import BaseNet
from my_lib.neural_module.transformer import ResFF
from typing import Any,Optional,Union

from config import *

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import Dataset
from torch_geometric.data import HeteroData
from torch_geometric.loader.data_list_loader import DataListLoader
from torch_geometric.utils import to_dense_batch
from torch_geometric.data.storage import (BaseStorage, NodeStorage,EdgeStorage)
from torch_geometric.nn.data_parallel import DataParallel
from torch_geometric.nn import HeteroConv,GraphNorm
import random
import numpy as np
import os
import logging
import pickle
import json
import codecs
from tqdm import tqdm
import pickle
import numpy as np
import pandas as pd
import math
from copy import deepcopy

logging.basicConfig(format='%(asctime)s : %(levelname)s : %(message)s', level=logging.INFO)

NodeOrEdgeStorage = Union[NodeStorage, EdgeStorage]

class Datax(HeteroData):
    # def __init__(self,
    #              graph_func_node=dict(x=None),
    #              graph_attr_node=dict(x=None),
    #              graph_glob_node=dict(x=None),
    #              ):
    #     super().__init__()
    #     self.graph_func_node=graph_func_node
    def __cat_dim__(self, key: str, value: Any,
                    store: Optional[NodeOrEdgeStorage] = None, *args,
                    **kwargs) -> Any:
        if bool(re.search('(token)', key)): #|map
            return None  # generate a new 0 dimension
        if bool(re.search('(pos)', key)):
            return -1
        return super().__cat_dim__(key, value,store)    #return不能漏了！！！

    # def __inc__(self, key: str, value: Any,
    #             store: Optional[NodeOrEdgeStorage] = None, *args,
    #             **kwargs) -> Any:
    #     if 'index' in key:
    #         print(store.size())
    #         return torch.tensor(store.size()).view(2, 1)
    #     else:
    #         return 0

    # @property
    # def num_nodes(self) -> Optional[int]:
    #     r"""Returns the number of nodes in the graph."""
    #     return sum([value.size(0) for value in self.x_dict.values()])
        # return super().num_nodes

class Datasetx(Dataset):
    '''
    文本对数据集对象（根据具体数据再修改）
    '''
    def __init__(self,
                 code_graphs,
                 texts=None,
                 ids=None,
                 text_max_len=None,
                 text_begin_idx=1,
                 text_end_idx=2,
                 pad_idx=0):
        self.len = len(code_graphs)  # 样本个数
        self.text_max_len = text_max_len
        self.text_begin_idx = text_begin_idx
        self.text_end_idx = text_end_idx

        # if code_max_len is None:
        #     self.code_max_len = max([len(item['code']['tokens']) for item in code_graphs])
        if text_max_len is None and texts is not None:
            self.text_max_len = max([len(text) for text in texts])  # 每个输出只是一个序列
        self.code_graphs = code_graphs
        self.texts = texts
        self.ids = ids
        self.pad_idx = pad_idx

    def __getitem__(self, index):
        if self.texts is None:
            pad_text_in = np.zeros((self.text_max_len + 1,), dtype=np.int64)  # decoder端的输入
            pad_text_in[0] = self.text_begin_idx
            pad_text_out = None
        else:
            tru_text = self.texts[index][:self.text_max_len]  # 先做截断
            pad_text_in = np.lib.pad(tru_text,
                                    (1, self.text_max_len - len(tru_text)),
                                    'constant',
                                    constant_values=(self.text_begin_idx, self.pad_idx))
            tru_text_out = np.lib.pad(tru_text,
                                     (0, 1),
                                     'constant',
                                     constant_values=(0, self.text_end_idx))  # padding
            pad_text_out = np.lib.pad(tru_text_out,
                                     (0, self.text_max_len + 1 - len(tru_text_out)),
                                     'constant',
                                     constant_values=(self.pad_idx, self.pad_idx))  # padding
            # pad_out_input=np.lib.pad(pad_out[:-1],(1,0),'constant',constant_values=(self.text_begin_idx, 0))
        data=Datax()
        data['nleaf'].x=torch.tensor(self.code_graphs[index]['nleaf_node_ids'])
        data['code'].x=torch.tensor(self.code_graphs[index]['code_node_ids'])
        data['code'].src_map=torch.tensor(self.code_graphs[index]['code2text_map_ids']).long()
        data['nleaf','base_father','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_father_nleaf_base_edges']).long()
        data['nleaf','base_child','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_child_nleaf_base_edges']).long()
        data['code','base_father','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['code_father_nleaf_base_edges']).long()
        data['nleaf','sibling_prev','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_prev_nleaf_sibling_edges']).long()        
        data['nleaf','sibling_next','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_next_nleaf_sibling_edges']).long()        
        data['code','sibling_prev','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['code_prev_nleaf_sibling_edges']).long()        
        data['code','sibling_next','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['code_next_nleaf_sibling_edges']).long()        
        data['nleaf','dfg_prev','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_prev_nleaf_dfg_edges']).long()        
        data['nleaf','dfg_next','nleaf'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_next_nleaf_dfg_edges']).long()
        data['nleaf','base_child','code'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_child_code_base_edges']).long()        
        data['nleaf','sibling_prev','code'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_prev_code_sibling_edges']).long()        
        data['nleaf','sibling_next','code'].edge_index=torch.tensor(self.code_graphs[index]['nleaf_next_code_sibling_edges']).long()        
        data['code','sibling_prev','code'].edge_index=torch.tensor(self.code_graphs[index]['code_prev_code_sibling_edges']).long()        
        data['code','sibling_next','code'].edge_index=torch.tensor(self.code_graphs[index]['code_next_code_sibling_edges']).long()        
        data['code','code_prev','code'].edge_index=torch.tensor(self.code_graphs[index]['code_prev_code_code_edges']).long()        
        data['code','code_next','code'].edge_index=torch.tensor(self.code_graphs[index]['code_next_code_code_edges']).long()        
        data['text'].text_token_input=torch.tensor(pad_text_in).long()
        if self.texts is not None:
            data['text'].text_token_output = torch.tensor(pad_text_out).long()
        data['text'].num_nodes = pad_text_in.shape[0]
        if self.ids is not None:
            data['idx'].idx=torch.tensor(self.ids[index])
            data['idx'].num_nodes=1
        # print(data.num_nodes)
        return data

    def __len__(self):
        return self.len

class CodeGraphEnc(nn.Module):
    def __init__(self,
                 emb_dims,
                 nleaf_max_num,
                 code_max_len,
                 graph_node_emb_op,
                #  code_mpos_voc_size,
                #  code_npos_voc_size,
                #  code_att_layers=2,
                #  code_att_heads=8,
                #  code_att_head_dims=None,
                #  code_ff_hid_dims=2048,
                 graph_gnn_layers=6,
                 graph_GNN=SAGEConv,
                 graph_gnn_aggr='mean',
                 drop_rate=0.,
                 **kwargs,
                 ):
        super().__init__()
        kwargs.setdefault('pad_idx', 0)
        self.pad_idx = kwargs['pad_idx']
        self.nleaf_max_num = nleaf_max_num
        self.code_max_len=code_max_len
        self.emb_dims=emb_dims

        # assert len(graph_sim_node_ids.shape)==1
        # graph_sim_node_voc_size=np.unique(graph_sim_node_ids).shape[0]
        # self.graph_node_to_sim_token_map_op=nn.Embedding.from_pretrained(torch.tensor(graph_sim_node_ids).view([-1,1]).float(),freeze=True,padding_idx=kwargs['pad_idx'])
        self.graph_node_emb_op = graph_node_emb_op
        # self.graph_node_to_sim_token_map_op=graph_node_to_sim_token_map_op
        # self.graph_node_emb_op = nn.Embedding(graph_node_voc_size, emb_dims, padding_idx=kwargs['pad_idx'])
        # self.sim_node_emb_op = nn.Embedding(graph_sim_node_voc_size, emb_dims, padding_idx=kwargs['pad_idx'])
        
        # self.code_mpos_emb_op = nn.Embedding(code_mpos_voc_size, emb_dims, padding_idx=kwargs['pad_idx'])
        # self.code_npos_emb_op = nn.Embedding(code_npos_voc_size, emb_dims, padding_idx=kwargs['pad_idx'])
        
        # nn.init.xavier_uniform_(self.graph_node_emb_op.weight[1:, ])
        # nn.init.xavier_uniform_(self.graph_sim_node_emb_op.weight[1:, ])
        # nn.init.xavier_uniform_(self.code_mpos_emb_op.weight[1:, ])
        # nn.init.xavier_uniform_(self.code_npos_emb_op.weight[1:, ])

        # self.graph_emb_norm_op = GraphNorm(emb_dims)
        self.emb_drop_op = nn.Dropout(p=drop_rate)
        # self.code_emb_norm_op = nn.LayerNorm(emb_dims)
        # self.graph_emb_norm_op = nn.LayerNorm(emb_dims)

        # self.code_enc_op = TranEnc(query_dims=emb_dims,
        #                             head_num=code_att_heads,
        #                             ff_hid_dims=code_ff_hid_dims,
        #                             head_dims=code_att_head_dims,
        #                             layer_num=code_att_layers,
        #                             drop_rate=drop_rate,
        #                             pad_idx=kwargs['pad_idx'])

        self.gnn_layers = graph_gnn_layers
        self.gnn_ops=nn.ModuleList()
        self.gnorm_ops=nn.ModuleList()
        self.grelu_ops=nn.ModuleList()
        edge_keys=[('nleaf','base_father','nleaf'),('nleaf','base_child','nleaf'),('code','base_father','nleaf'),
                    ('nleaf','sibling_prev','nleaf'),('nleaf','sibling_next','nleaf'),('code','sibling_prev','nleaf'),('code','sibling_next','nleaf'),
                    ('nleaf','dfg_prev','nleaf'),('nleaf','dfg_next','nleaf'),
                    ('nleaf','base_child','code'),
                    ('nleaf','sibling_prev','code'),('nleaf','sibling_next','code'),('code','sibling_prev','code'),('code','sibling_next','code'),
                    ('code','code_prev','code'),('code','code_next','code')]
        node_keys=['nleaf','code']
        # if graph_GNN==SAGEConv:
        root_weights=[True,False,False,False,False,False,False,False,False,True,False,False,False,False,False,False]
        for _ in range(graph_gnn_layers):
            if graph_GNN==SAGEConv:
                gnn_dict=dict([(edge_key,graph_GNN((emb_dims,emb_dims), emb_dims, aggr=graph_gnn_aggr,root_weight=root_weight)) for edge_key,root_weight in zip(edge_keys,root_weights)])
            elif graph_GNN==TransformerConv:
                gnn_dict=dict([(edge_key,graph_GNN((emb_dims,emb_dims), out_channels=emb_dims//8,heads=8, aggr=graph_gnn_aggr,dropout=drop_rate,root_weight=root_weight)) for edge_key,root_weight in zip(edge_keys,root_weights)])
                assert emb_dims/8==emb_dims//8
            else:
                gnn_dict=dict([(edge_key,graph_GNN((emb_dims,emb_dims), emb_dims, aggr=graph_gnn_aggr)) for edge_key in edge_keys])
            gnn=HeteroConv(gnn_dict,aggr='sum')
            self.gnn_ops.append(gnn)
            grelu_dict=dict([(node_key,nn.Sequential(nn.ReLU(), nn.Dropout(p=drop_rate))) for node_key in node_keys])
            self.grelu_ops.append(nn.ModuleDict(grelu_dict))
            gnorm_dict=dict([(node_key,GraphNorm(emb_dims)) for node_key in node_keys])
            self.gnorm_ops.append(nn.ModuleDict(gnorm_dict))

    def forward(self, data):
        assert len(data['nleaf'].x.size()) == 1  #[batch_graph_nleaf_num,]
        assert len(data['code'].src_map.size())==1 #[batch_graph_code_num,]
        assert len(data.edge_index_dict[('nleaf','base_father','nleaf')].size()) == 2  # 点是一堆节点序号[2,batch_xx_edge_num]

        # graph_node_emb=self.graph_node_emb_op(data.x_dict['node'])  ##[batch_graph_node_num,emb_dims]
        # # graph_node_emb[sim_node_mask==True,:]=graph_node_emb[sim_node_mask==True,:].add(sim_node_emb)*0.5
        # data['node'].x=self.emb_drop_op(graph_node_emb) ##[batch_graph_node_num,emb_dims]
        data['nleaf'].x=self.emb_drop_op(self.graph_node_emb_op(data.x_dict['nleaf']))  #[batch_graph_nleaf_node_num,emb_dims]
        data['code'].x=self.emb_drop_op(self.graph_node_emb_op(data.x_dict['code']))  #[batch_graph_code_node_num,emb_dims]

        # graph_node_emb2=data['node'].x.clone()
        # code_emb=data['node'].x[data['node'].code_mask==True,:]* np.sqrt(self.emb_dims) ##[batch_leaf_node_num,emb_dims]
        # data['node'].x=self.graph_emb_norm_op(data['node'].x) ##[batch_graph_node_num,emb_dims]
        # code_mpos_emb=self.code_mpos_emb_op(data['node'].code_pos[0,:][data['node'].code_mask==True])     #[batch_leaf_node_num,emb_dims]
        # code_npos_emb=self.code_npos_emb_op(data['node'].code_pos[1,:][data['node'].code_mask==True])     #[batch_leaf_node_num,emb_dims]
        # code_pos_emb=self.emb_drop_op(code_mpos_emb.add(code_npos_emb)) #[batch_leaf_node_num,emb_dims]

        # code_x_batch=data.x_batch_dict['node'][data['node'].code_mask==True]    #[batch_leaf_node_num,]
        
        # code_emb,code_mask=to_dense_batch(code_emb,
        #                                 batch=code_x_batch,
        #                                 fill_value=self.pad_idx,
        #                                 max_num_nodes=self.code_max_len)    #[batch_size,code_max_len,emb_dims],[batch_size,code_max_len]
        # code_pos_emb,_=to_dense_batch(code_pos_emb,
        #                                 batch=code_x_batch,
        #                                 fill_value=self.pad_idx,
        #                                 max_num_nodes=self.code_max_len)    #[batch_size,code_max_len,emb_dims],[batch_size,code_max_len]
        # code_emb=self.code_emb_norm_op(code_emb.add(code_pos_emb))   #[batch_size,code_max_len,emb_dims]
        # code_enc=self.code_enc_op(query=code_emb,query_mask=code_mask)  # [batch_data_num,code_max_len,emb_dims]
        # sparse_code_enc=code_enc.contiguous().view(-1,code_enc.size(-1))[code_mask.view(-1)==True,:] ###[batch_leaf_node_num,emb_dims] convert dense batch into sparse batch
        # data['node'].x[data['node'].code_mask==True,:]=data['node'].x[data['node'].code_mask==True,:].add(sparse_code_enc)  #[batch_leaf_node_num,emb_dims]
        
        
        # =code_emb
        # graph_node_emb=data['node'].x.clone()
        for gnn,relu_dict,norm_dict in zip(self.gnn_ops,self.grelu_ops,self.gnorm_ops):
            x_dict=gnn(x_dict=data.x_dict,edge_index_dict=data.edge_index_dict)   # dict(xx_node:[batch_xx_node_num,hid_dims])
            for node_key,x in x_dict.items():
                data[node_key].x=norm_dict[node_key](data[node_key].x.add(relu_dict[node_key](x)))  #,batch=data.x_batch_dict[node_key]
            # data['node'].x=norm(data['node'].x.add(relu(x_dict['node'])),batch=data.x_batch_dict['node']) #data[key].x residual connection
        
        # data['node'].x=graph_node_emb2.add(data['node'].x)
        
        nleaf_enc,_=to_dense_batch(data.x_dict['nleaf'],
                                  batch=data.x_batch_dict['nleaf'], #data['nleaf'].x_batch也可以
                                  fill_value=self.pad_idx,
                                  max_num_nodes=self.nleaf_max_num)  #[batch_data_num,nleaf_max_num,emb_dims],[batch_size,nleaf_max_num]
        code_enc,_=to_dense_batch(data.x_dict['code'],
                                    batch=data.x_batch_dict['code'],  # data['leaf'].x_batch也可以
                                    fill_value=self.pad_idx,
                                    max_num_nodes=self.code_max_len)    # [batch_data_num,code_max_len]
        code_src_map,_=to_dense_batch(data['code'].src_map,
                                        batch=data.x_batch_dict['code'],  # data['leaf'].x_batch也可以
                                        fill_value=self.pad_idx,
                                        max_num_nodes=self.code_max_len)    # [batch_data_num,code_max_len]                                   

        return nleaf_enc,code_enc,code_src_map

class Dec(nn.Module):
    def __init__(self,
                 emb_dims,
                 text_voc_size,
                 text_emb_op,
                 text_max_len,
                 enc_out_dims,
                 att_layers,
                 att_heads,
                 att_head_dims=None,
                 ff_hid_dims=2048,
                 drop_rate=0.,
                 **kwargs
                 ):
        super().__init__()
        kwargs.setdefault('pad_idx', 0)
        kwargs.setdefault('copy', True)
        self._copy = kwargs['copy']
        self.emb_dims = emb_dims
        self.text_voc_size = text_voc_size
        # embedding dims为text_voc_size+2*code_max_len

        # assert len(text_sim_token_ids.shape)==1
        # text_sim_token_voc_size=np.unique(text_sim_token_ids).shape[0]
        # self.text_token_to_sim_token_map_op=nn.Embedding.from_pretrained(torch.tensor(text_sim_token_ids).view([-1,1]).float(),freeze=True,padding_idx=kwargs['pad_idx'])
        # self.text_token_to_sim_token_map_op=text_token_to_sim_token_map_op
        self.text_emb_op = text_emb_op
        # self.text_emb_op = nn.Embedding(text_voc_size + code_max_len, emb_dims, padding_idx=kwargs['pad_idx'])
        # self.sim_token_emb_op = nn.Embedding(text_sim_token_voc_size, emb_dims, padding_idx=kwargs['pad_idx'])
        # nn.init.xavier_uniform_(self.text_emb_op.weight[1:, ])
        # nn.init.xavier_uniform_(self.sim_token_emb_op.weight[1:, ])
        self.pos_encoding = PosEnc(max_len=text_max_len+1, emb_dims=emb_dims, train=True, pad=True,pad_idx=kwargs['pad_idx'])  #不要忘了+1,因为输入前加了begin_id
        # nn.init.xavier_uniform_(self.pos_encoding.weight[1:, ])
        self.emb_layer_norm = nn.LayerNorm(emb_dims)
        # self.text_dec_op = TranDec(query_dims=emb_dims,
        #                            key_dims=enc_out_dims,
        #                            head_nums=att_heads,
        #                            head_dims=att_head_dims,
        #                            layer_num=att_layers,
        #                            ff_hid_dims=ff_hid_dims,
        #                            drop_rate=drop_rate,
        #                            pad_idx=kwargs['pad_idx'],
        #                            self_causality=True)
        self.text_dec_op = DualTranDec(query_dims=emb_dims,
                                        key_dims=enc_out_dims,
                                        head_num=att_heads,
                                        ff_hid_dims=ff_hid_dims,
                                        head_dims=att_head_dims,
                                        layer_num=att_layers,
                                        drop_rate=drop_rate,
                                        pad_idx=kwargs['pad_idx'],
                                        mode='sequential',
                                        self_causality=True)
        self.dropout = nn.Dropout(p=drop_rate)
        self.out_fc = nn.Linear(emb_dims, text_voc_size)
        self.copy_generator = MultiCopyGenerator(tgt_dims=emb_dims,
                                                     tgt_voc_size=text_voc_size,
                                                     src_dims=enc_out_dims,
                                                     att_heads=att_heads,
                                                     att_head_dims=att_head_dims,
                                                     drop_rate=drop_rate,
                                                     pad_idx=kwargs['pad_idx'])

    def forward(self,nleaf_enc,code_enc,code_src_map,text_input):
        text_emb = self.text_emb_op(text_input)   # (B,L_text,D_text_emb)
        # text_emb[sim_token_mask==True,:]=text_emb[sim_token_mask==True,:].add(sim_token_emb)*0.5
        text_emb=text_emb* np.sqrt(self.emb_dims)
        pos_emb = self.pos_encoding(text_input)  # # (B,L_text,D_emb)
        text_dec = self.dropout(text_emb.add(pos_emb))  # (B,L_text,D_emb)
        text_dec = self.emb_layer_norm(text_dec)  # (B,L_text,D_emb)

        nleaf_mask = nleaf_enc.abs().sum(-1).sign()  # (batch_size,nleaf_max_num)
        code_mask = code_enc.abs().sum(-1).sign() # (batch_size,code_max_len)
        text_mask = text_input.abs().sign()  # (B,L_text)
        text_dec = self.text_dec_op(query=text_dec,
                                    key1=code_enc,
                                    key2=nleaf_enc,
                                    query_mask=text_mask,
                                    key_mask1=code_mask,
                                    key_mask2=nleaf_mask
                                    )  # (B,L_text,D_text_emb)

        if not self._copy:
            text_output = self.out_fc(text_dec)  # (B,L_text,text_voc_size)包含begin_idx和end_idx
            # text_output = F.softmax(text_output, dim=-1)
            # text_output[:,:,-1]=0.    #不生成begin_idx，默认该位在text_voc_size最后一个，置0
        else:
            # text_output=F.pad(text_output,(0,2*self.text_max_len)) #pad last dim
            text_output = self.copy_generator(text_dec,
                                             code_enc,code_src_map)
        # text_output[:, :, self.text_voc_size - 1] = 0.  # 不生成begin_idx，默认该位在text_voc_size最后一个，置0
        # text_output[:, :, 0] = 0.  # pad位不生成
        return text_output.transpose(1, 2)

class TNet(BaseNet):
    def __init__(self,
                 emb_dims,
                 nleaf_max_num,
                 code_max_len,
                 text_max_len,
                 io_voc_size,
                #  code_mpos_voc_size,
                #  code_npos_voc_size,
                 text_voc_size,
                #  code_att_layers=2,
                #  code_att_heads=8,
                #  code_att_head_dims=None,
                #  code_ff_hid_dims=2048,
                 graph_gnn_layers=6,
                 graph_GNN=SAGEConv,
                 graph_gnn_aggr='add',
                 text_att_layers=3,
                 text_att_heads=8,
                 text_att_head_dims=None,
                 text_ff_hid_dims=2048,
                 drop_rate=0.,
                 **kwargs,
                 ):
        super().__init__()
        kwargs.setdefault('copy', True)
        kwargs.setdefault('pad_idx', 0)  # GraphData.batch to_dense_data用的
        self.init_params = locals()
        io_token_emb_op=nn.Embedding(io_voc_size, emb_dims, padding_idx=kwargs['pad_idx'])
        nn.init.xavier_uniform_(io_token_emb_op.weight[1:, ])
        self.enc_op = CodeGraphEnc(emb_dims=emb_dims,
                                    nleaf_max_num=nleaf_max_num,
                                    code_max_len=code_max_len,
                                    # graph_node_voc_size=graph_node_voc_size,
                                    graph_node_emb_op=io_token_emb_op,
                                    # graph_node_to_sim_token_map_op=io_token_to_sim_token_map_op,
                                    # code_mpos_voc_size=code_mpos_voc_size,
                                    # code_npos_voc_size=code_npos_voc_size,
                                    # code_att_layers=code_att_layers,
                                    # code_att_heads=code_att_heads,
                                    # code_att_head_dims=code_att_head_dims,
                                    # code_ff_hid_dims=code_ff_hid_dims,
                                    graph_gnn_layers=graph_gnn_layers,
                                    graph_GNN=graph_GNN,
                                    graph_gnn_aggr=graph_gnn_aggr,
                                    drop_rate=drop_rate,
                                    pad_idx=kwargs['pad_idx'])
        self.dec_op = Dec(emb_dims=emb_dims,
                            text_voc_size=text_voc_size,
                            text_max_len=text_max_len,
                            # code_max_len=code_max_len,
                            text_emb_op=io_token_emb_op,
                            # text_token_to_sim_token_map_op=io_token_to_sim_token_map_op,
                            enc_out_dims=emb_dims,
                            att_layers=text_att_layers,
                            att_heads=text_att_heads,
                            att_head_dims=text_att_head_dims,
                            ff_hid_dims=text_ff_hid_dims,
                            drop_rate=drop_rate,
                            copy=kwargs['copy'],
                            pad_idx=kwargs['pad_idx'])

    def forward(self, code_graph):
        text_input=code_graph['text'].text_token_input.clone()
        del code_graph['text']
        nleaf_enc,code_enc,code_src_map = self.enc_op(data=code_graph)
        text_output = self.dec_op(nleaf_enc=nleaf_enc,code_enc=code_enc,
                                    code_src_map=code_src_map,
                                    text_input=text_input)
        return text_output

class TModel(TransSeq2Seq):
    def __init__(self,
                 model_dir,
                 model_name='Transformer_based_model',
                 model_id=None,
                 emb_dims=512,
                #  code_att_layers=3,
                #  code_att_heads=8,
                #  code_att_head_dims=None,
                #  code_ff_hid_dims=2048,
                 graph_gnn_layers=3,
                 graph_GNN=SAGEConv,
                 graph_gnn_aggr='add',
                 text_att_layers=3,
                 text_att_heads=8,
                 text_att_head_dims=None,
                 text_ff_hid_dims=2048,
                 drop_rate=0.,
                 copy=True,
                 pad_idx=0,
                 train_batch_size=32,
                 pred_batch_size=32,
                 max_train_size=-1,
                 max_valid_size=32 * 10,
                 max_big_epochs=20,
                 regular_rate=1e-5,
                 lr_base=0.001,
                 lr_decay=0.9,
                 min_lr_rate=0.01,
                 warm_big_epochs=2,
                 start_valid_epoch=20,
                 early_stop=20,
                 Net=TNet,
                 Dataset=Datasetx,
                 beam_width=1,
                 train_metrics=[get_sent_bleu],
                 valid_metric=get_sent_bleu,
                 test_metrics=[get_sent_bleu],
                 train_mode=True,
                 **kwargs
                 ):
        logging.info('Construct %s' % model_name)
        super().__init__(model_name=model_name,
                         model_dir=model_dir,
                         model_id=model_id)
        self.init_params = locals()
        # self.sim_token_ids=sim_token_ids
        self.emb_dims = emb_dims
        # self.code_att_layers = code_att_layers
        # self.code_att_heads = code_att_heads
        # self.code_att_head_dims = code_att_head_dims
        # self.code_ff_hid_dims = code_ff_hid_dims
        self.graph_gnn_layers = graph_gnn_layers
        self.graph_GNN = graph_GNN
        self.graph_gnn_aggr = graph_gnn_aggr
        self.text_att_layers = text_att_layers
        self.text_att_heads = text_att_heads
        self.text_att_head_dims = text_att_head_dims
        self.text_ff_hid_dims = text_ff_hid_dims
        self.drop_rate = drop_rate
        self.pad_idx = pad_idx
        self.copy = copy
        self.train_batch_size = train_batch_size
        self.pred_batch_size = pred_batch_size
        self.max_train_size = max_train_size
        self.max_valid_size = max_valid_size
        self.max_big_epochs = max_big_epochs
        self.regular_rate = regular_rate
        self.lr_base = lr_base
        self.lr_decay = lr_decay
        self.min_lr_rate = min_lr_rate
        self.warm_big_epochs = warm_big_epochs
        self.start_valid_epoch=start_valid_epoch
        self.early_stop=early_stop
        self.Net = Net
        self.Dataset = Dataset
        self.beam_width = beam_width
        self.train_metrics = train_metrics
        self.valid_metric = valid_metric
        self.test_metrics = test_metrics
        self.train_mode = train_mode

    def _logging_paramerter_num(self):
        logging.info("{} have {} paramerters in total".format(self.model_name, sum(
            x.numel() for x in self.net.parameters() if x.requires_grad)))
        # 计算enc+dec的parameter总数
        code_graph_enc_param_num = sum(x.numel() for x in self.net.module.enc_op.gnn_ops.parameters() if x.requires_grad) + \
                                    sum(x.numel() for x in self.net.module.enc_op.gnorm_ops.parameters() if x.requires_grad) + \
                                    sum(x.numel() for x in self.net.module.enc_op.grelu_ops.parameters() if x.requires_grad)

        text_dec_param_num = sum(x.numel() for x in self.net.module.dec_op.text_dec_op.parameters() if x.requires_grad)
                            # sum(x.numel() for x in self.net.module.dec_op.copy_generator.parameters() if x.requires_grad)
        enc_dec_param_num = code_graph_enc_param_num + text_dec_param_num
        logging.info("{} have {} paramerters in encoder and decoder".format(self.model_name, enc_dec_param_num))

    def fit(self,
            train_data,
            valid_data,
            **kwargs
            ):
        self.nleaf_max_num=0
        self.code_max_len = 0
        self.io_voc_size = 0
        # self.code_mpos_voc_size = 0
        # self.code_npos_voc_size = 0
        self.text_max_len=0
        for code_graph,text in zip(train_data['code_graphs'],train_data['texts']):
            self.nleaf_max_num = max(self.nleaf_max_num,len(code_graph['nleaf_node_ids']))
            self.code_max_len = max(self.code_max_len,len(code_graph['code_node_ids']))
            self.io_voc_size = max(self.io_voc_size,max(code_graph['nleaf_node_ids']+code_graph['code_node_ids']+text))
            # self.code_mpos_voc_size = max(self.code_mpos_voc_size,np.max(code_graph['node_in_code_poses'][0,:]))
            # self.code_npos_voc_size = max(self.code_npos_voc_size,np.max(code_graph['node_in_code_poses'][1,:]))
            self.text_max_len=max(self.text_max_len,len(text))
        self.io_voc_size+=1
        # self.code_mpos_voc_size+=1
        # self.code_npos_voc_size+=1

        self.text_voc_size = len(train_data['text_dic']['text_i2w'])  # 包含了begin_idx和end_idx
        # self.io_voc_size=max(self.io_voc_size,self.text_voc_size+self.code_max_len)
        
        # print(self.nleaf_max_num, self.code_max_len,self.text_max_len,
        #       self.io_voc_size, self.text_voc_size,
        #       self.code_mpos_voc_size,self.code_npos_voc_size)

        net = self.Net(
                        emb_dims=self.emb_dims,
                        nleaf_max_num=self.nleaf_max_num,
                        code_max_len=self.code_max_len,
                        text_max_len=self.text_max_len,
                        io_voc_size=self.io_voc_size,
                        #    code_mpos_voc_size=self.code_mpos_voc_size,
                        #    code_npos_voc_size=self.code_npos_voc_size,
                        text_voc_size=self.text_voc_size,
                        #    code_att_layers=self.code_att_layers,
                        #    code_att_heads=self.code_att_heads,
                        #    code_att_head_dims=self.code_att_head_dims,
                        #    code_ff_hid_dims=self.code_ff_hid_dims,
                        graph_gnn_layers=self.graph_gnn_layers,
                        graph_GNN=self.graph_GNN,
                        graph_gnn_aggr=self.graph_gnn_aggr,
                        text_att_layers=self.text_att_layers,
                        text_att_heads=self.text_att_heads,
                        text_att_head_dims=self.text_att_head_dims,
                        text_ff_hid_dims=self.text_ff_hid_dims,
                        drop_rate=self.drop_rate,
                        pad_idx=self.pad_idx,
                        copy=self.copy
                       )

        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')  # 选择GPU优先

        self.net =DataParallel(net.to(device),follow_batch=['x'])  # 并行使用多GPU
        # self.net = BalancedDataParallel(0, net.to(device), dim=0)  # 并行使用多GPU
        # self.net = net.to(device)  # 数据转移到设备
        self._logging_paramerter_num()  # 需要有并行的self.net和self.model_name
        self.net.train()  # 设置网络为训练模式

        self.optimizer = optim.Adam(self.net.parameters(),
                                    lr=self.lr_base,
                                    weight_decay=self.regular_rate)


        self.criterion = LabelSmoothSoftmaxCEV2(reduction='mean', ignore_index=self.pad_idx, label_smooth=0.0)
        # self.criterion = nn.NLLLoss(ignore_index=self.pad_idx)

        self.text_begin_idx = self.text_voc_size - 1
        self.text_end_idx = self.text_voc_size - 2
        self.tgt_begin_idx,self.tgt_end_idx=self.text_begin_idx,self.text_end_idx
        assert train_data['text_dic']['text_i2w'][self.text_end_idx] == OUT_END_TOKEN
        assert train_data['text_dic']['text_i2w'][self.text_begin_idx] == OUT_BEGIN_TOKEN  # 最后两个是end_idx 和begin_idx

        self.max_train_size = len(train_data['code_graphs']) if self.max_train_size == -1 else self.max_train_size
        train_code_graphs, train_texts,train_ids = zip(*random.sample(list(zip(train_data['code_graphs'], train_data['texts'],train_data['ids'])),
                                                     min(self.max_train_size,
                                                         len(train_data['code_graphs']))
                                                     )
                                      )

        train_set = self.Dataset(code_graphs=train_code_graphs,
                                 texts=train_texts,
                                 ids=train_ids,
                                 text_max_len=self.text_max_len,
                                 text_begin_idx=self.text_begin_idx,
                                 text_end_idx=self.text_end_idx,
                                 pad_idx=self.pad_idx)
        # train_loader = DataLoader(dataset=train_set,
        #                           train_batch_size=self.train_batch_size,
        #                           shuffle=True,
        #                           follow_batch=['graph_node', 'graph_node_after'])
        train_loader=DataListLoader(dataset=train_set,
                                    batch_size=self.train_batch_size,
                                    shuffle=True,
                                    drop_last=True)

        if self.warm_big_epochs is None:
            self.warm_big_epochs = max(self.max_big_epochs // 10, 2)
        self.scheduler = LrWarmUp(self.optimizer,
                                  min_rate=self.min_lr_rate,
                                  lr_decay=self.lr_decay,
                                  warm_steps=self.warm_big_epochs * len(train_loader),
                                  # max(self.max_big_epochs//10,2)*train_loader.__len__()
                                  reduce_steps=len(train_loader))  # 预热次数 train_loader.__len__()
        if self.train_mode:  # 如果进行训练
            # best_net_path = os.path.join(self.model_dir, '{}_best_net.net'.format(self.model_name))
            # self.net.load_state_dict(torch.load(best_net_path))
            # self.net.train()
            # torch.cuda.empty_cache()
            for i in range(0,self.max_big_epochs):
                # logging.info('---------Train big epoch %d/%d' % (i + 1, self.max_big_epochs))
                pbar = tqdm(train_loader)
                for batch_data in pbar:
                    batch_text_output = []
                    ids=[]
                    for data in batch_data:
                        batch_text_output.append(data['text'].text_token_output.unsqueeze(0))
                        del data['text'].text_token_output
                        ids.append(data['idx'].idx.item())
                        del data['idx']

                    batch_text_output = torch.cat(batch_text_output, dim=0).to(device)
                    # print(batch_text_output[:2,:])
                    pred_text_output = self.net(batch_data)

                    loss = self.criterion(pred_text_output, batch_text_output)  # 计算loss
                    self.optimizer.zero_grad()  # 梯度置0
                    loss.backward()  # 反向传播
                    # clip_grad_norm_(self.net.parameters(),1e-2)  #减弱梯度爆炸
                    self.optimizer.step()  # 优化
                    self.scheduler.step()  # 衰减

                    # log_info = '[Big epoch:{}/{}]'.format(i + 1, self.max_big_epochs)
                    # if i+1>=self.start_valid_epoch:
                    text_dic = {'text_i2w': train_data['text_dic']['text_i2w'],
                               'ex_text_i2ws': [train_data['text_dic']['ex_text_i2ws'][k] for k in ids]}
                    log_info=self._get_log_fit_eval(loss=loss,
                                                    pred_tgt=pred_text_output,
                                                    gold_tgt=batch_text_output,
                                                    tgt_i2w=text_dic
                                                    )
                    log_info = '[Big epoch:{}/{},{}]'.format(i + 1, self.max_big_epochs, log_info)
                    pbar.set_description(log_info)
                    del pred_text_output,batch_text_output,batch_data

                del pbar
                if i+1 >= self.start_valid_epoch:
                    self.max_valid_size = len(valid_data['code_graphs']) if self.max_valid_size == -1 else self.max_valid_size
                    valid_srcs, valid_tgts, ex_text_i2ws = zip(*random.sample(list(zip(valid_data['code_graphs'],
                                                                                       valid_data['texts'],
                                                                                       valid_data['text_dic']['ex_text_i2ws'])),
                                                                              min(self.max_valid_size,
                                                                                  len(valid_data['code_graphs']))
                                                                              )
                                                               )
                    text_dic = {'text_i2w': train_data['text_dic']['text_i2w'],
                                'ex_text_i2ws': ex_text_i2ws}
                    # torch.cuda.empty_cache()
                    worse_epochs = self._do_validation(valid_srcs=valid_srcs,  # valid_data['code_graphs']
                                                       valid_tgts=valid_tgts,  # valid_data['texts']
                                                       tgt_i2w=text_dic,  # valid_data['text_dic']
                                                       increase_better=True,
                                                       last=False)  # 根据验证集loss选择best_net
                    # worse_epochs = self._do_validation(valid_srcs=valid_data['code_graphs'],  #
                    #                                    valid_tgts=valid_data['texts'],  #
                    #                                    tgt_i2w=valid_data['text_dic'],  #
                    #                                    increase_better=True,
                    #                                    last=False)  # 根据验证集loss选择best_net
                    if worse_epochs>=self.early_stop:
                        break
        # torch.cuda.empty_cache()
        self._do_validation(valid_srcs=valid_data['code_graphs'],
                            valid_tgts=valid_data['texts'],
                            tgt_i2w=valid_data['text_dic'],
                            increase_better=True,
                            last=True)  # 根据验证集loss选择best_net
        self._logging_paramerter_num()  # 需要有并行的self.net和self.model_name

    def predict(self,
                code_graphs,
                text_dic):
        logging.info('Predict outputs of %s' % self.model_name)
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')  # 选择GPU优先
        # self.net = self.net.to(device)  # 数据转移到设备,不重新赋值不行
        self.net.eval()  # 切换测试模式
        enc_op=DataParallel(self.net.module.enc_op,follow_batch=['x'])
        dec_op=torch.nn.DataParallel(self.net.module.dec_op)
        # enc.eval()
        # dec.eval()
        data_set = self.Dataset(code_graphs=code_graphs,
                                texts=None,
                                ids=None,
                                text_max_len=self.text_max_len,
                                text_begin_idx=self.text_begin_idx,
                                text_end_idx=self.text_end_idx,
                                pad_idx=self.pad_idx)  # 数据集，没有out，不需要id

        data_loader = DataListLoader(dataset=data_set,
                                     batch_size=self.pred_batch_size,   #1.5,2.5
                                     shuffle=False)
                                 # follow_batch=['graph_node', 'graph_node_after'])  # data loader
        pred_text_id_np_batches = []  # 所有batch的预测出的id np
        with torch.no_grad():  # 取消梯度
            pbar = tqdm(data_loader)
            for batch_data in pbar:
                # 从batch_data图里把解码器输入输出端数据调出来
                batch_text_input = []
                for data in batch_data:
                    batch_text_input.append(data['text'].text_token_input.unsqueeze(0))
                    del data['text']
                batch_text_input = torch.cat(batch_text_input, dim=0).to(device)

                # 先跑encoder，生成编码
                batch_nleaf_enc,batch_code_enc,batch_code_src_map=enc_op(batch_data)
                batch_text_output: list = []  # 每步的output tensor
                if self.beam_width == 1:
                    for i in range(self.text_max_len + 1):  # 每步开启
                        pred_out = dec_op(nleaf_enc=batch_nleaf_enc,code_enc=batch_code_enc,code_src_map=batch_code_src_map,text_input=batch_text_input)  # 预测该步输出 (B,text_voc_size,L_text)
                        batch_text_output.append(pred_out[:, :, i].unsqueeze(-1).to('cpu').data.numpy())  # 将该步输出加入msg output
                        if i < self.text_max_len:  # 如果没到最后，将id加入input
                            batch_text_input[:, i + 1] = torch.argmax(pred_out[:, :, i], dim=1)
                    batch_pred_text = np.concatenate(batch_text_output, axis=-1)[:, :, :-1]  # (B,D_tgt,L_tgt)
                    batch_pred_text[:, self.tgt_begin_idx, :] = -np.inf  # (B,D_tgt,L_tgt)
                    batch_pred_text[:, self.pad_idx, :] = -np.inf  # (B,D_tgt,L_tgt)
                    batch_pred_text_np = np.argmax(batch_pred_text, axis=1)  # (B,L_tgt) 要除去pad id和begin id
                    pred_text_id_np_batches.append(batch_pred_text_np)  # [(B,L_tgt)]
                else:
                    batch_pred_text=trans_beam_search(net=dec_op,
                                                      beam_width=self.beam_width,
                                                      dec_input_arg_name='text_input',
                                                      length_penalty=1,
                                                      begin_idx=self.tgt_begin_idx,
                                                      pad_idx=self.pad_idx,
                                                      end_idx=self.tgt_end_idx,
                                                      nleaf_enc=batch_nleaf_enc,
                                                      code_enc=batch_code_enc,
                                                      code_src_map=batch_code_src_map,
                                                      text_input=batch_text_input
                                                      )     # (B,L_tgt)

                    pred_text_id_np_batches.append(batch_pred_text.to('cpu').data.numpy()[:,:-1])  # [(B,L_tgt)]

        pred_text_id_np = np.concatenate(pred_text_id_np_batches,axis=0)  # (AB,tgt_voc_size,L_tgy)
        self.net.train()  # 切换回训练模式
        # pred_texts=[[{**text_dic['text_i2w'],**text_dic['ex_text_i2ws'][j]}[i] for ]]
        # 利用字典将msg转为token
        pred_texts = self._tgt_ids2tokens(pred_text_id_np, text_dic, self.text_end_idx)

        return pred_texts  # 序列概率输出形状为（A,D)
    
    def generate_texts(self,code_graphs,text_dic,res_path,gold_texts,raw_data,token_data,**kwargs):
        '''
        生成src对应的tgt并保存
        :param code_graphs:
        :param text_dic:
        :param res_path:
        :param kwargs:
        :return:
        '''
        logging.info('>>>>>>>Generate the targets according to sources and save the result to {}'.format(res_path))
        kwargs.setdefault('beam_width',1)
        res_dir=os.path.dirname(res_path)
        if not os.path.exists(res_dir):
            os.makedirs(res_dir)
        pred_texts=self.predict(code_graphs=code_graphs,
                                text_dic=text_dic
                                )
        # codes=map(lambda x:x['code']['tokens'],code_graphs)
        # codes=self._code_ids2tokens(codes,code_i2w,self.pad_idx)
        gold_texts=self._tgt_ids2tokens(gold_texts,text_dic,self.pad_idx)
        res_data = []
        for i,(pred_text,gold_text,raw_item,token_item) in \
                enumerate(zip(pred_texts,gold_texts,raw_data,token_data)):
            sent_bleu=self.valid_metric([pred_text],[gold_text])
            res_data.append(dict(pred_text=' '.join(pred_text),
                                 gold_text=' '.join(gold_text),
                                 sent_bleu=sent_bleu,
                                 raw_code=raw_item['code'],
                                 raw_text=raw_item['text'],
                                 id=raw_item['id'],
                                 token_text=token_item['text'],
                                 ))
        # res_df=pd.DataFrame(res_dic).T
        # # print(res_df)
        # excel_writer = pd.ExcelWriter(res_path)  # 根据路径savePath打开一个excel写文件
        # res_df.to_excel(excel_writer,header=True,index=True)
        # excel_writer.save()
        with codecs.open(res_path,'w',encoding='utf-8') as f:
            json.dump(res_data,f,indent=4, ensure_ascii=False)
        self._logging_paramerter_num()  # 需要有并行的self.net和self.model_name
        logging.info('>>>>>>>The result has been saved to {}'.format(res_path))

    def _code_ids2tokens(self,code_idss, code_i2w, end_idx):
        return [[code_i2w[idx] for idx in (code_ids[:code_ids.tolist().index(end_idx)]
                                                    if end_idx in code_ids else code_ids)]
                          for code_ids in code_idss]
    
    def _tgt_ids2tokens(self, text_id_np, text_dic, end_idx=0, **kwargs):
        if self.copy:
            text_tokens: list = []
            for j, text_ids in enumerate(text_id_np):
                text_i2w = {**text_dic['text_i2w'], **text_dic['ex_text_i2ws'][j]}
                end_i = text_ids.tolist().index(end_idx) if end_idx in text_ids else len(text_ids)
                text_tokens.append([text_i2w[text_idx] for text_idx in text_ids[:end_i]])
                # if end_i == 0:
                #     print()
        else:
            text_i2w=text_dic['text_i2w']
            text_tokens = [[text_i2w[idx] for idx in (text_ids[:text_ids.tolist().index(end_idx)]
                                                      if end_idx in text_ids else text_ids)]
                          for text_ids in text_id_np]

        return text_tokens

if __name__ == '__main__':

    logging.info('Parameters are listed below: \n'+'\n'.join(['{}: {}'.format(key,value) for key,value in params.items()]))

    model = TModel(
                    # sim_token_ids=np.load(io_token_sim_id_path),
                    model_dir=params['model_dir'],
                   model_name=params['model_name'],
                   model_id=params['model_id'],
                   emb_dims=params['emb_dims'],
                #    code_att_layers=params['code_att_layers'],
                #    code_att_heads=params['code_att_heads'],
                #    code_att_head_dims=params['code_att_head_dims'],
                #    code_ff_hid_dims=params['code_ff_hid_dims'],
                   graph_gnn_layers=params['graph_gnn_layers'],
                   graph_GNN=params['graph_GNN'],
                   graph_gnn_aggr=params['graph_gnn_aggr'],
                   text_att_layers=params['text_att_layers'],
                   text_att_heads=params['text_att_heads'],
                   text_att_head_dims=params['text_att_head_dims'],
                   text_ff_hid_dims=params['text_ff_hid_dims'],
                   drop_rate=params['drop_rate'],
                   copy=params['copy'],
                   pad_idx=params['pad_idx'],
                   train_batch_size=params['train_batch_size'],
                   pred_batch_size=params['pred_batch_size'],
                   max_train_size=params['max_train_size'],  # -1 means all
                   max_valid_size=params['max_valid_size'],  ####################10
                   max_big_epochs=params['max_big_epochs'],
                   regular_rate=params['regular_rate'],
                   lr_base=params['lr_base'],
                   lr_decay=params['lr_decay'],
                   min_lr_rate=params['min_lr_rate'],
                   warm_big_epochs=params['warm_big_epochs'],
                   early_stop=params['early_stop'],
                   start_valid_epoch=params['start_valid_epoch'],
                   Net=TNet,
                   Dataset=Datasetx,
                   beam_width=params['beam_width'],
                   train_metrics=train_metrics,
                   valid_metric=valid_metric,
                   test_metrics=test_metrics,
                   train_mode=params['train_mode'])

    logging.info('Load data ...')
    # print(train_avail_data_path)
    with codecs.open(train_avail_data_path, 'rb') as f:
        train_data = pickle.load(f)
    with codecs.open(valid_avail_data_path, 'rb') as f:
        valid_data = pickle.load(f)
    with codecs.open(test_avail_data_path, 'rb') as f:
        test_data = pickle.load(f)
    # io_token_sim_ids=np.load(io_token_sim_id_path)

    # with codecs.open(code_node_i2w_path, 'rb') as f:
    #     code_i2w = pickle.load(f)

    with codecs.open(test_token_data_path,'r') as f:
        test_token_data=json.load(f)

    with codecs.open(test_raw_data_path,'r') as f:
        test_raw_data=json.load(f)

    # train_data['code_graphs']=train_data['code_graphs'][:1000]
    # train_data['texts']=train_data['texts'][:1000]
    # train_data['ids']=train_data['ids'][:1000]

    # print(len(train_data['texts']), len(valid_data['texts']), len(test_data['texts']))
    model.fit(train_data=train_data,
              valid_data=valid_data)

    for key, value in params.items():
        logging.info('{}: {}'.format(key, value))
    logging.info('Parameters are listed below: \n'+'\n'.join(['{}: {}'.format(key,value) for key,value in params.items()]))

    model.generate_texts(code_graphs=test_data['code_graphs'],
                         text_dic=test_data['text_dic'],
                         res_path=res_path,
                         # code_i2w=code_i2w, d
                         gold_texts=test_data['texts'],
                         raw_data=test_raw_data,
                         token_data=test_token_data)

    keep_test_data_ids=[]
    with open(keep_test_data_id_path,'r') as f:
        for line in f:
            keep_test_data_ids.append(int(line.strip()))

    with open(res_path,'r') as f:
        res_data=json.load(f)
    gold_texts=[]
    pred_texts=[]
    # sblues=[]
    for i,item in enumerate(res_data):
        if i in keep_test_data_ids:
            assert item['id']==i
            gold_text=item['gold_text'].split()
            pred_text=item['pred_text'].split()
            gold_texts.append([gold_text])
            pred_texts.append(pred_text)
    print('The performance on the cleand Python(GypSum) testing set is:\n')
    print(get_meteor.__name__,':',get_meteor(pred_texts,gold_texts))
    print(get_rouge.__name__,':',get_rouge(pred_texts,gold_texts))
    # print(len(pred_texts),len(gold_texts))
    print(get_sent_bleu1.__name__,':',get_sent_bleu1(pred_texts,gold_texts))
    print(get_sent_bleu2.__name__,':',get_sent_bleu2(pred_texts,gold_texts))
    print(get_sent_bleu3.__name__,':',get_sent_bleu3(pred_texts,gold_texts))
    print(get_sent_bleu4.__name__,':',get_sent_bleu4(pred_texts,gold_texts))
    print(get_sent_bleu.__name__,':',get_sent_bleu(pred_texts,gold_texts))