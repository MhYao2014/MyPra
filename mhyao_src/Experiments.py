import sys
sys.path.append('../')
import operator
import torch
import torch.optim as optim
import torch.nn as nn
import random, os
from pathlib import Path
from collections import defaultdict
from torch.utils.data import DataLoader
from GraphManager import ProcessedGraphManager
from PRAModel import LogisticRegression, PRAModelWrapper
from temp.GetFeature import GetFeature
from temp.data_utils import PRAData
from tqdm import tqdm
from multiprocessing import Pool
import pdb
import concurrent.futures



class GraphExperiments:
    def __init__(self,
                 query_graph_pt: ProcessedGraphManager,
                 predict_graph_pt: ProcessedGraphManager = None,
                 model_pt: PRAModelWrapper = None,
                 hit_range: int = 10):
        self.query_graph_pt = query_graph_pt
        self.predict_graph_pt = predict_graph_pt
        self.model_pt = model_pt
        self.hit_range = hit_range
        self.hit_percent = None
        self.MR = None
        self.MRR = None

    def parallel_among_fact_list(self, part_of_fact_list):
        hits = 0
        mr = 0
        mrr = 0
        bad_relation_count = 0
        for triple in tqdm(part_of_fact_list):
            if triple[2] not in self.model_pt.relation_torch_model_dict:
                bad_relation_count += 1
                continue
            else:
                tail_mid_list = list(self.query_graph_pt.entity_set)
                entity_rank_dict = self.model_pt.rank_score(head_mid=triple[0],
                                                            relation=triple[2],
                                                            tail_mid_list=tail_mid_list)
                rank_tail_sorted = sorted(entity_rank_dict.items(), key=lambda item: item[1], reverse=False)
                for rank, (tail, score) in enumerate(rank_tail_sorted):
                    if triple[2] == tail:
                        if rank < self.hit_range:
                            hits += 1
                        mr += rank
                        mrr += 1 / rank
                        break
        return hits, mr, mrr, bad_relation_count

    def tail_predict(self):
        process_num = 5
        process_pool = Pool(processes=process_num)
        hit_mr_mrr_results_list = []
        batch_size = len(self.predict_graph_pt.fact_list) // process_num
        for batch_id in range(process_num):
            last_fact_idx = min((batch_id+1)*batch_size, len(self.predict_graph_pt.fact_list))
            part_of_fact_lists = self.predict_graph_pt.fact_list[batch_id*batch_size:last_fact_idx]
            hit_mr_mrr_results_list.append(process_pool.apply_async(func=self.parallel_among_fact_list,
                                                                    args=(part_of_fact_lists,)).get())
        process_pool.close()
        process_pool.join()
        hits = 0
        mr = 0
        mrr = 0
        bad_relation_count = 0
        for (tmp_hits, tmp_mr, tmp_mrr, tmp_bad_relation_count) in hit_mr_mrr_results_list:
            hits += tmp_hits
            mr += tmp_mr
            mrr += tmp_mrr
            bad_relation_count += tmp_bad_relation_count
        self.hit_percent = hits / (len(self.predict_graph_pt.fact_list) - bad_relation_count)
        self.MR = mr / (len(self.predict_graph_pt.fact_list) - bad_relation_count)
        self.MRR = mrr / (len(self.predict_graph_pt.fact_list) - bad_relation_count)
        return self.hit_percent, self.MR, self.MRR


class Validation(GraphExperiments):
    def __init__(self,
                 model_pt: PRAModelWrapper,
                 query_graph_pt: ProcessedGraphManager,
                 predict_graph_pt: ProcessedGraphManager,
                 hit_range):
        super().__init__(model_pt=model_pt,
                         query_graph_pt=query_graph_pt,
                         predict_graph_pt=predict_graph_pt)
        self.hit_range = hit_range


class Test(GraphExperiments):
    def __init__(self,
                 query_graph_pt: ProcessedGraphManager,
                 predict_graph_pt: ProcessedGraphManager,
                 hit_range,
                 model_pt: LogisticRegression = None):
        super().__init__(model_pt=model_pt,
                         query_graph_pt=query_graph_pt,
                         predict_graph_pt=predict_graph_pt)
        self.hit_range = hit_range

    def find_best_results(self,
                          results_list):
        return results_list[0]


class PRATrain(GraphExperiments):
    def __init__(self,
                 query_graph: ProcessedGraphManager,
                 neg_pairs_path: str,
                 meta_path_file: str,
                 hold_out_path: Path,
                 hyper_param: float):
        super().__init__(query_graph_pt=query_graph)
        self.meta_path_file = meta_path_file
        self.hold_out_path = hold_out_path
        self.relation_meta_paths = defaultdict(list)
        self.alpha = hyper_param
        self.neg_pairs_path = neg_pairs_path
        self.model_pt = PRAModelWrapper(query_graph_pt=self.query_graph_pt)

    def get_relation_paths(self):
        self.relation_meta_paths = defaultdict(list)
        with open(self.meta_path_file, "r") as f:
            datas = f.readlines()
            for data in datas:
                data = data.strip("\n").split("\t")
                query_relation = data[1]
                meta_path = data[3:]
                self.relation_meta_paths[query_relation].append(meta_path)
        self.query_graph_pt.relation_meta_paths = self.relation_meta_paths

    def get_neg_pairs(self):
        return ProcessedGraphManager(file_path=self.neg_pairs_path).fact_list

    def load_model_from_file(self):
        raise NotImplementedError

    def train_this_hold_out(self, if_save_model: bool=False,
                            hold_out_id: int=None):
        if hold_out_id is not None and hold_out_id == 0: # 只有主进程输出信息
            print(f"超参alpha为{self.alpha},开始训练{self.query_graph_pt.file_path}中的三元组:")
        self.get_relation_paths()  # get all the pre-computed meta paths
        neg_pairs = self.get_neg_pairs()  # get all the negative triples
        relation_count = 0
        for relation in self.query_graph_pt.relation_set:
            relation_count += 1
            # 若关系没有对应的metapath,那么就忽略这个关系,不为其生成对应模型
            if relation not in self.relation_meta_paths.keys():
                continue
            else:
                # 生成正负样本
                relation_pos_pairs = self.query_graph_pt.relation_pos_sample_dict[relation]
                train_pairs_01 = []
                pos_pairs_num = len(relation_pos_pairs)
                if pos_pairs_num >= 4000:
                    relation_pos_pairs = relation_pos_pairs[0:5000]
                pos_pairs_num = len(relation_pos_pairs)
                random_num = 1 + random.random()
                sample_num = int(pos_pairs_num * random_num)
                if sample_num > len(neg_pairs):
                    sample_num = len(neg_pairs)
                neg_pairs_01 = random.sample(neg_pairs, sample_num)
                for item in relation_pos_pairs:
                    e1, e2 = item
                    train_pairs_01.append([e1, e2, 1])
                for item in neg_pairs_01:
                    e1, e2, _ = item
                    train_pairs_01.append([e1, e2, 0])
                if hold_out_id is not None and hold_out_id == 0:  # 只有主进程输出信息
                    print(f"预测关系:{relation}, 是第:{relation_count}个关系, 该关系数据数量:{len(train_pairs_01)}")
                # 生成样本特征
                feature = GetFeature(tuple_data=self.query_graph_pt.fact_list,
                                 entity_pairs=train_pairs_01,
                                 metapath=self.relation_meta_paths[relation])
                data_feature_dict = feature.get_probs(hold_out_id)
                metapath_len = len(self.relation_meta_paths[relation])
                input_size = metapath_len
                learning_rate = 0.001
                batch_size = 4
                epoch_num = 1
                pra_data = PRAData(data_feature_dict=data_feature_dict,
                                   metapath_len=metapath_len)
                train_loader = DataLoader(pra_data, batch_size=batch_size)
                self.model_pt.relation_torch_model_dict[relation] = LogisticRegression(input_size=input_size, num_classes=1)
                criterion = nn.BCELoss()
                optimizer = optim.SGD(self.model_pt.relation_torch_model_dict[relation].parameters(), lr=learning_rate)

                for epoch in range(epoch_num):
                    for i, (path_feature, label) in enumerate(train_loader):
                        optimizer.zero_grad()
                        outputs = self.model_pt.relation_torch_model_dict[relation](path_feature).squeeze(dim=1)
                        loss = criterion(outputs, label)
                        loss.backward()
                        optimizer.step()
                        if (i + 1) % 500 == 0 and hold_out_id is not None and hold_out_id == 0:  # 只有主进程输出信息
                            print('\t\tEpoch: [%d/%d], Step:[%d/%d], Loss: %.4f'
                                  % (epoch + 1, epoch_num, i + 1, len(pra_data) // batch_size, loss.data))
                if if_save_model is True and hold_out_id is not None and hold_out_id == 0:
                    print(f"\t为关系{relation}保存模型.")
                    model_save_path = self.hold_out_path / 'model/'
                    if os.path.exists(model_save_path) is False:
                        os.makedirs(model_save_path)
                    model_save_path = model_save_path / (relation.replace("/", '') + f"_{self.alpha}_model.pkl")
                    torch.save(self.model_pt.relation_torch_model_dict[relation].state_dict(), model_save_path)
