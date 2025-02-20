import logging
import random
from collections import defaultdict

from data import Block, Message
from network import (
    ERR_OUTAGE,
    AdHocNetwork,
    GetDataMsg,
    INVMsg,
    Network,
    Packet,
    DataSegment,
    TopologyNetwork,
    TPPacket,
)

from .._consts import _IDLE, FLOODING, OUTER_RCV_MSG, SELF_GEN_MSG, SELFISH, SPEC_TARGETS, SYNC_LOC_CHAIN
from .nic_abc import NetworkInterface

logger = logging.getLogger(__name__)


class NICWithTp(NetworkInterface):
    def __init__(self, miner) -> None:
        super().__init__(miner)
        self._neighbors:list[int] = []
        # 暂存本轮收到的数据包
        self._segment_buffer = defaultdict(list[Message])
        # 输出队列(拓扑网络)
        self._output_queues = defaultdict(list[Message])
        self._channel_states = {}

    def nic_join_network(self, network):
        self._network = network
        self.init_queues()

    def init_queues(self):
        if len(self._neighbors) == 0:
            return
        # 初始化发送队列
        for neighbor in self._neighbors:
            self._output_queues[neighbor] = []
            self._channel_states[neighbor] = _IDLE

    def remove_neighbor(self, remove_id:int):
        """断开连接，从neighbor列表移除
        """
        if remove_id not in self._neighbors:
            logger.warning("M%d: remove neighbour M%d Failed! not connected", 
                           self.miner_id, remove_id)
            return
        self._neighbors = [n for n in self._neighbors if n != remove_id]
        if self._channel_states[remove_id] != _IDLE:
            self._output_queues[remove_id].insert(0, self._channel_states[remove_id])
        if len(self._output_queues[remove_id]) == 0:
            self._output_queues.pop(remove_id, None)
        self._channel_states.pop(remove_id, None)
        
        # logger.info("M%d: removed neighbour M%d", self.miner_id, remove_id)

    def get_number_of_segments_to_send(self, source, target):
        return self._network.get_number_of_segments_to_send(source, target)

    def add_neighbor(self, add_id:int, round):
        if add_id in self._neighbors:
            logger.warning("M%d: add neighbour M%d Failed! already connected", 
                           self.miner.miner_id,add_id)
            return
        self._neighbors.append(add_id)
        self._channel_states[add_id] = _IDLE
        if add_id not in self._output_queues.keys():
            self._output_queues[add_id] = []
        # logger.info("round%d, M%d: added neighbour M%d", 
        #             round, self.miner.miner_id, add_id)
        self.gossip_full_chain(add_id, round)
       

    def nic_receive(self, packet: Packet):
        '''处理接收到的消息, 直接调用miner.receive'''
        self._receive_buffer.append(packet)
        payload = packet.payload
        if  not (isinstance(payload, list) and isinstance(payload[0], DataSegment)):
            return self.miner.receive(payload)

        rcv_states = {}
        def update_rcv_states(block_name, rcv_state):
            if block_name in rcv_states and rcv_states[block_name] is True:
                return
            rcv_states[block_name] = rcv_state

        for seg in payload:
            if not isinstance(seg.origin_block, Block):
                raise TypeError("Segment not contains a block!")
            block_name = seg.origin_block.name
            # 如果这个段中的区块已经收到过了，就不再处理
            if self.miner.in_local_chain(seg.origin_block):
                update_rcv_states(seg.origin_block.name, False)
                continue
            self._segment_buffer[block_name].append(seg.seg_id)
            if len(set(self._segment_buffer[block_name])) != seg.origin_block.segment_num:
                update_rcv_states(seg.origin_block.name, False)
                continue
            self._segment_buffer.pop(block_name)
            logger.info("M%d: All %d segments of %s collected", self.miner.miner_id, 
                        seg.origin_block.segment_num, seg.origin_block.name)
            update_rcv_states(seg.origin_block.name, self.miner.receive(seg.origin_block))
        return rcv_states

    def _get_segids_not_rcv(self, block:Block):
        seg_ids = self._segment_buffer[block.name]
        return [sid for sid in list(range(block.segment_num)) if sid not in seg_ids]
    
    def forward_buffer_to_output_queue(self, msg_source_type):
        for [msg, strategy, spec_tgts] in self._forward_buffer[msg_source_type]:
            targets = self.select_target(msg, strategy, spec_tgts) # 选择目标节点
            out_msgs = self.seg_blocks(msg) if (isinstance(msg, Block) 
                and self._network.withSegments) else [msg]
            for target in  targets:
                self._output_queues[target].extend(out_msgs)

    def seg_blocks(self, block:Block):
        segids = list(range(block.segment_num))
        random.shuffle(segids)
        return [DataSegment(block, sid) for sid in segids]

    def nic_forward(self, round):
        # 将 forward_buffer 写入 output_queue 中
        if (len(self._forward_buffer[SELF_GEN_MSG]) != 0 or
            len(self._forward_buffer[OUTER_RCV_MSG]) != 0):
            self.forward_buffer_to_output_queue(SELF_GEN_MSG)
            self.forward_buffer_to_output_queue(OUTER_RCV_MSG)
            logger.info(
                "round %d, M%d, neighbors %s, outputqueue %s", 
                round, self.miner_id, str(self._neighbors), 
                {k:[msg.name if isinstance(msg, Message) else msg for msg in v] 
                 for k,v in self._output_queues.items()}
            )
        # 向邻居节点发送 output_queue 中的消息
        for neighbor in self._neighbors:
            if self._channel_states[neighbor] != _IDLE:
                continue
            que = self._output_queues[neighbor]
            if len(que) == 0:
                continue
            
            while len(self._output_queues[neighbor]) > 0:
                msg = que.pop(0)
                msg_name = (msg.name if isinstance(msg, Block) 
                    else  str((msg.origin_block.name, msg.seg_id)) 
                    if isinstance(msg, DataSegment)  else "other msg")
                logger.info("round %d, M%d->M%d, try to send %s", 
                            round, self.miner_id, neighbor, msg_name)
                if msg == SYNC_LOC_CHAIN:
                    self.gossip_full_chain(neighbor, round)
                    while (len(self._output_queues[neighbor]) != 0 and 
                           self._output_queues[neighbor][0] == SYNC_LOC_CHAIN):
                        self._output_queues[neighbor].pop(0)
                    if len(self._output_queues[neighbor]) == 0:
                        break
                    msg = self._output_queues[neighbor].pop(0)
                
                isMsgRequired = self.gossip_single_msg(neighbor, msg, round)
                if not isMsgRequired:
                    continue

                send_msgs =[msg]
                
                if isinstance(msg, DataSegment):
                    seg_nums = self.get_number_of_segments_to_send(self.miner_id, neighbor) - 1
                    while (seg_nums > 0 and len(self._output_queues[neighbor]) > 0 and 
                           isinstance(self._output_queues[neighbor][0], DataSegment)):
                        send_msg = self._output_queues[neighbor].pop(0)
                        if self.gossip_single_msg(neighbor, send_msg, round):
                            send_msgs.append(send_msg)
                            seg_nums = seg_nums - 1
                
                self._channel_states[neighbor] = send_msgs
                self.send_data(send_msgs, neighbor, round, sendTogether=True)
                break
        
        self.clear_forward_buffer()

    def send_inv(self, inv:INVMsg, round:int ):
        getDataReply = GetDataMsg(require=False) # 空getData，回应的getData会写入该结构中
        self._network.access_network([inv, getDataReply], self.miner.miner_id,  round, inv.target)
        # logger.info("round %d, Sending inv , get reqblocks %s", 
        #             round, str([b.name for b in getDataReply.req_blocks])) 
        return getDataReply
    
    def gossip_single_msg(self, target, msg:Message, round):
        """
        发送某个消息前，询问对方是否需要
        """
        if not isinstance(msg, Block) and not isinstance(msg, DataSegment):
            return True
        # 通过inv询问对方是否需要该区块
        
        inv = INVMsg(self.miner_id, target, msg, isFullChain=False)
        getDataReply  = self.send_inv(inv, round)
        logger.info("round%d, M%d->M%d: %s require: %s", round, 
                    self.miner_id, target, msg, getDataReply.isRequired)
        return getDataReply.isRequired
    
    def gossip_full_chain(self, target:int, round:int):
        """
        新建连接或挖出新区块时和邻居对齐整链, inv消息包含本地lastblock
        """
        last_block = self.miner.get_local_chain().get_last_block()
        inv = INVMsg(self.miner_id, target, last_block, isFullChain=True)
        getDataReply  = self.send_inv(inv, round)
        if not getDataReply.isRequired:
            return
        if inv.target not in self._output_queues.keys():
            self._output_queues[inv.target] = []
        # logger.info("round%d, M%d -> M%d: getData %s", round, target, self.miner_id, 
        #              str([req_b.name for req_b in getDataReply.req_blocks]))
        if not self._network.withSegments:
            for req_b in getDataReply.req_blocks:
                self._output_queues[inv.target].append(req_b)
            return
        # 将需要的分段加入输出队列
        for (req_b, segids) in getDataReply.req_segs:
            random.shuffle(segids)
            for sid in segids:
                self._output_queues[inv.target].append(DataSegment(req_b, sid))
        # 将完整区块分段后加入输出队列
        for req_b in getDataReply.req_blocks:
            segids = list(range(req_b.segment_num))
            random.shuffle(segids)
            for sid in segids:
                self._output_queues[inv.target].append(DataSegment(req_b, sid))
            
    def reply_getdata(self, inv:INVMsg):
        """
        接收到inv消息后, 返回getData, 包含自己需要的区块
        """
        getData = GetDataMsg(self.miner.miner_id, inv.source, [inv.block_or_seg])

        if isinstance(inv.block_or_seg, DataSegment):
            req_b = inv.block_or_seg.origin_block
            if req_b.name in self._segment_buffer.keys():
                s = self._get_segids_not_rcv(req_b)
                getData.isRequired = inv.block_or_seg.seg_id in s
            else:
                getData.isRequired =  not self.miner.in_local_chain(req_b)
            if not getData.isRequired:
                logger.info("M%d->M%d: %d, %s %s", inv.source, self.miner_id, req_b.segment_num, self._segment_buffer[req_b.name], self._get_segids_not_rcv(req_b))
            return getData

        getData.isRequired = not self.miner.in_local_chain(inv.block_or_seg)

        if not inv.isFullChain:
            return getData
        
        if getData.isRequired is False:
            return getData
        
        # 返回需要的区块列表
        getData.isRequired = False
        # inv高度低于本地直接返回
        inv_h =  inv.block_or_seg.get_height()
        loc_chain = self.miner.get_local_chain()
        loc_h = loc_chain.get_last_block().get_height()
        if inv_h < loc_h:
            if not self.miner.isAdversary:
                return getData
            else:
                getData.isRequired = True
                getData.req_blocks = [inv.block_or_seg]
                return getData
        getData.isRequired = True
        getData.req_blocks = []
        req_b = inv.block_or_seg
        while req_b is not None and not loc_chain.search_block(req_b):
            if self._network.withSegments and req_b.name in self._segment_buffer.keys():
                # 已经收到部分分段，请求需要的分段
                sids_notrcv = self._get_segids_not_rcv(req_b)
                getData.req_segs.append((req_b, sids_notrcv))
            else:
                getData.req_blocks.append(req_b)
            req_b = req_b.parentblock
        return getData
    
    def send_data(self, msgs:list[Message], target:int,round:int, sendTogether:bool=False):
        """
        inv没问题后发送数据
        """
        self._network.access_network(msgs, self.miner_id, round, target, sendTogether)
            
    def select_target(self, msg:Message=None, strategy:str=FLOODING, spec_tgts:list=None):
        if strategy == FLOODING:
            return self.select_target_flooding(msg)
        if strategy == SPEC_TARGETS:
            if spec_tgts is None or len(spec_tgts) == 0:
                raise ValueError("Please specify the targets(SPEC_TARGETS)")
            return self.select_target_spec(msg, spec_tgts)
        if strategy == SELFISH:
            return []

    def select_target_flooding(self, msg:Block=None):
        """
        泛洪转发, 转发给不包括source的邻居节点
        """
        targets = []
        msg_from = -1
        # 目标节点不包括区块的来源
        if msg is not None and msg != SYNC_LOC_CHAIN and isinstance(msg, Block):
            for packet in self._receive_buffer:
                if not isinstance(packet.payload, Block):
                    continue
                if msg.name == packet.payload.name:
                    msg_from = packet.source
                    break
        targets = [n for n in self._neighbors if n != msg_from]
        return targets
    

    def select_target_spec(self, msg:Block=None, spec_tgts:list = None):
        """
        转发给指定节点
        """
        targets = [t for t in spec_tgts if t in self._neighbors]
        msg_from = -1
        # 目标节点不包括区块的来源
        if msg is not None and msg != SYNC_LOC_CHAIN and isinstance(msg, Block):
            for packet in self._receive_buffer:
                if not isinstance(packet.payload, Block):
                    continue
                if msg.name == packet.payload.name:
                    msg_from = packet.source
                    break
        targets = [t for t in spec_tgts if t in self._neighbors and t != msg_from]
        return targets

    
    def get_reply(self, msg_name, target:int, err:str, round):
        """
        消息发送完成后，用于接收是否发送成功的回复
        """
        # 传输成功即将信道状态置为空闲
        if err is None:
            logger.info("round %d, M%d -> M%d: Forward  %s success!", 
                    round, self.miner_id, target, msg_name)
            self._channel_states[target]=_IDLE
            return
        # 信道中断将msg重新放回队列，等待下轮重新发送
        if err == ERR_OUTAGE:
            logger.info("round %d, M%d -> M%d: Forward  %s failed: link outage", 
                    round, self.miner_id, target, msg_name)
            sending_msgs = self._channel_states[target] 
            self._channel_states[target] = _IDLE
            for msg in sending_msgs:
                self._output_queues[target].insert(0, msg)
            # self._output_queues[target].insert(0, sending_msgs)
            return