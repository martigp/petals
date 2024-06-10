from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging
import random
import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Sequence, Set, Union
from weakref import WeakMethod
import os
from enum import Enum

import dijkstar
import numpy as np
from hivemind import DHT, P2P, MSGPackSerializer, PeerID
from multiaddr import Multiaddr
from hivemind.dht.node import Blacklist
from hivemind.moe.client.remote_expert_worker import RemoteExpertWorker
from hivemind.proto import runtime_pb2
from hivemind.utils.logging import get_logger

from petals.client.config import ClientConfig
from petals.client.routing.sequence_info import RemoteSequenceInfo
from petals.client.routing.spending_policy import NoSpendingPolicy
from petals.data_structures import ModuleUID, RemoteSpanInfo, ServerState
from petals.server.handler import TransformerConnectionHandler
from petals.utils.dht import get_remote_module_infos
from petals.utils.ping import PingAggregator
from petals.utils.random import sample_up_to

logger = get_logger(__name__)

class Reputation:
    """
    A peer reputation. Defined by the number of agreements / number of checks.
    num_agrees starts as 1
    num_checks starts as 2
    """
    def __init__ (self, num_agrees : float = 1.0, num_checks : float = 2.0, trusted : bool = False):
        self.num_agrees : float  = num_agrees
        self.num_checks : float = num_checks
        self.trusted : bool = trusted

    def add_disagree(self):
        if not self.trusted:
            self.num_checks += 1

    def add_agree(self):
        if not self.trusted:
            self.num_checks += 1
            self.num_agrees += 1
    
    def get_reputation(self) -> float:
        if self.trusted:
            # Make this a number that is greater than 1 but won't far outweight
            # other peers with good reputation ~0.8 (0.667 >= in steady state)
            return 3.0
        else:
            return self.num_agrees / self.num_checks
    
class PeerReputations:
    """
    Represents the representations of every peer. Agreements and disagreements
    are registered through this class and this class will return the reputation
    of a given peer.
    """
    def __init__ (self):
        self.peer_reputations : Dict[PeerID, Reputation] = dict()
    
    def register_disagreement(self, peer : PeerID):
        if peer not in self.peer_reputations:
            self.peer_reputations[peer] = Reputation()
        self.peer_reputations[peer].add_disagree()
    
    def register_agreement(self, peer : PeerID):
        if peer not in self.peer_reputations:
            self.peer_reputations[peer] = Reputation()
        self.peer_reputations[peer].add_agree()
    
    def get_peer_reputation(self, peer : PeerID) -> float:
        if peer not in self.peer_reputations:
            self.peer_reputations[peer] = Reputation()
        return self.peer_reputations[peer].get_reputation()
    
    def is_peer_trusted(self, peer : PeerID) -> bool:
        return self.peer_reputations[peer].trusted

    def add_peer(self, peer: PeerID, trusted : bool = False):
        self.peer_reputations[peer] = Reputation(trusted)

class DisagreementStatus(Enum):
    NO_DISAGREE = 1,
    PEER_A_TRUSTED = 2,
    PEER_B_TRUSTED = 3,
    BOTH_UNTRUSTED = 4


class SequenceManagerConfig(ClientConfig):
    def __init__(self, *args, **kwargs):
        warnings.warn(
            "petals.client.routing.SequenceManagerConfig has been moved to petals.ClientConfig. "
            "This alias will be removed in Petals 2.2.0+",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)


@dataclasses.dataclass
class SequenceManagerState:
    p2p: P2P = None
    sequence_info: Optional[RemoteSequenceInfo] = None
    rpc_info: Optional[dict] = None
    banned_peers: Optional[Blacklist] = None
    reputations: Optional[PeerReputations] = None
    trusted_peers : Optional[set[PeerID]] = None
    path_replication : Optional[float] = None
    reputation_weight : Optional[float] = None

    def __getitem__(self, ix: Union[int, slice]) -> SequenceManagerState:
        return dataclasses.replace(self, sequence_info=self.sequence_info[ix])

    def __len__(self) -> int:
        return len(self.sequence_info)


class RemoteSequenceManager:
    """
    Sequence manager is a thread that keeps track of remote servers that hold the specified sequence of blocks.
    TL;DR it tells you, which peers you should ask to get a specific layer. It is used in RemoteSequential.
    When created, RemoteSequenceManager looks up which servers serve necessary layers by reading from DHT.
    Using this information, sequence manager can form sequences of servers that collectively have the full sequence.
    To form such a sequence, call .make_sequence with the appropriate optimization policy (see make_sequence docstr).

    :note: RemoteSequenceManager takes up some CPU and network I/O to operate in background. It is recommended to avoid
      running redundant sequence managers for the same set of layers.
    """

    def __init__(
        self,
        config: ClientConfig,
        block_uids: Sequence[ModuleUID],
        *,
        dht: Optional[DHT] = None,
        state: Optional[SequenceManagerState] = None,
    ):
        assert config.initial_peers or dht is not None, "Please specify `config.initial_peers` or `dht`"
        assert config.dht_prefix, "Could not find dht_prefix in config, please create model with dht_prefix=..."
        assert len(block_uids) > 0, "Sequences must contain at least one block"

        self.config = config
        if state is None:
            state = SequenceManagerState()
        self.state = state

        if dht is None:
            dht = DHT(
                initial_peers=config.initial_peers,
                client_mode=True,
                num_workers=32,
                startup_timeout=config.daemon_startup_timeout,
                start=True,
            )
        assert isinstance(dht, DHT) and dht.is_alive(), "`dht` must be a running hivemind.DHT instance"
        self.dht = dht

        if state.p2p is None:
            state.p2p = RemoteExpertWorker.run_coroutine(dht.replicate_p2p())
        
        if self.state.reputations is None:
            self.state.reputations = PeerReputations()
        
        # Gordon Added
        if self.state.trusted_peers is None:
            trusted_peers_str : str = os.environ.get("TRUSTED_PEERS")
            if trusted_peers_str != None:
                trusted_peers = trusted_peers_str.split(',')
                self.trusted_peers = set([PeerID.from_base58(Multiaddr(item)["p2p"]) for item in trusted_peers])
                for trusted_peer in self.trusted_peers:
                    self.state.reputations.add_peer(trusted_peer, trusted=True)
                    logger.info(f"GORDON: trusted peer peerid {trusted_peer.to_string()}")
            else:
                logger.debug(f"GORDON: no trusted_peers provided")
                self.trusted_peers = []

        if self.state.path_replication is None:
            path_replication : str = os.environ.get("PATH_REPLICATION")
            if path_replication != None:
                path_replication = float(path_replication)
                if path_replication < 0 or path_replication > 1:
                    logger.warn(f"GORDON: Provided invalid PATH_REPLICATION {self.state.path_replication}, must be [0,1]")
                    path_replication = 0.5
                self.state.path_replication = path_replication
            else:
                self.state.path_repliction = 0.5
            logger.debug(f"GORDON: Set path replication to {self.state.path_replication} ")
        
        if self.state.reputation_weight is None:
            reputation_weight : str = os.environ.get("REPUTATION_WEIGHT")
            if reputation_weight != None:
                reputation_weight = float(reputation_weight)
                if reputation_weight < 0 or reputation_weight > 1:
                    logger.warn(f"GORDON: Provided invalid REPUTATION_WEIGHT {self.state.reputation_weight}, must be [0,1]")
                    reputation_weight = 0
                self.state.reputation_weight = reputation_weight
            else:
                self.state.reputation_weight = 0
            logger.debug(f"GORDON: Set reputation weight to {self.state.reputation_weight} ")


        self.lock_changes = threading.Lock()
        self._thread = _SequenceManagerUpdateThread(config.update_period, WeakMethod(self._update))
        self._thread_start_lock = threading.Lock()
        self.policy = NoSpendingPolicy()

        self.allowed_servers = self._peer_ids_to_set(config.allowed_servers)
        self.blocked_servers = self._peer_ids_to_set(config.blocked_servers)

        self.ping_aggregator = PingAggregator(dht)

        if state.banned_peers is None:
            state.banned_peers = Blacklist(base_time=config.ban_timeout, backoff_rate=2.0)
        if state.sequence_info is None:
            state.sequence_info = RemoteSequenceInfo.make_empty(block_uids)

        if state.sequence_info.last_updated_time is not None:
            assert block_uids == state.sequence_info.block_uids
            self._thread.ready.set()  # no need to await the first dht fetch

    @staticmethod
    def _peer_ids_to_set(peer_ids: Optional[Sequence[Union[PeerID, str]]]) -> Optional[Set[PeerID]]:
        if peer_ids is None:
            return None

        result = set()
        for peer_id in peer_ids:
            if isinstance(peer_id, PeerID):
                result.add(peer_id)
            elif isinstance(peer_id, str):
                result.add(PeerID.from_base58(peer_id))
            else:
                raise TypeError(
                    f"`allowed_servers` and `blocked_servers` have to contain only PeerIDs or strings, but got {type(peer_id)}"
                )
        return result

    def make_sequence(
        self,
        start_index: int = 0,
        end_index: Optional[int] = None,
        *,
        mode: str,
        cache_tokens_needed: Optional[int] = None,
    ) -> List[RemoteSpanInfo]:
        """
        Form a sequence of remote servers that collectively serve all consecutive layers

        :param start_index: optional index of the first module in a sequence, default = the first of block_uids
        :param end_index: optional index of the last module (non-inclusive), default = after last of block uids
        :param mode: one of ["max_throughput", "min_latency"]
        """
        with self._thread_start_lock:
            if not self.is_alive():
                self._thread.start()
        if not self.ready.is_set():
            self.update(wait=True)  # this will await an existing update or trigger a new one (if not updating)

        end_index = end_index if end_index is not None else len(self)

        if mode == "min_latency":
            span_sequence = self._make_sequence_with_min_latency(
                start_index, end_index, cache_tokens_needed=cache_tokens_needed
            )
        elif mode == "max_throughput":
            span_sequence = self._make_sequence_with_max_throughput(start_index, end_index)
        else:
            raise RuntimeError(f"Unexpected mode {mode}")

        if self.config.show_route is True or (mode == "min_latency" and self.config.show_route == "inference"):
            route_repr = " => ".join(
                [f"{span.start}:{span.end} via …{str(span.peer_id)[-6:]}" for span in span_sequence]
            )
            logger.info(f"Route found: {route_repr}")
        return span_sequence
    
    def make_second_replacement_sequence(self,
        start : int = 0,
        end_index: Optional[int] = None,
        *,
        mode: str,
        cache_tokens_needed: Optional[int] = None,
        currentPath : List[RemoteSpanInfo] = []
    ) -> List[RemoteSpanInfo] :
        with self._thread_start_lock:
            if not self.is_alive():
                self._thread.start()
        if not self.ready.is_set():
            self.update(wait=True)  # this will await an existing update or trigger a new one (if not updating)

        end_index = end_index if end_index is not None else len(self)
        candidates = []
        client_server_rtts = self.ping_aggregator.to_dict()
        new_path = []
        for span in currentPath:
            if (np.random.random() > self.state.path_replication):
                new_path.append(span)
                continue
            
            logger.info(f"Gordon: Chose to replicate span from {span.start}->{span.end}")
            matching_spans = self.state.sequence_info.spans_containing_block[span.start]
            if not matching_spans:
                logger.info(f"Gordon: couldn't find a replacement for block {span.start}")
                new_path.append(span)
                continue
                
            # Check that at least one of these ends
            candidates = []
            candidate_weights = []
            for matching_span in matching_spans:
                if matching_span.end >= span.end and matching_span.peer_id != span.peer_id:
                    matching_span = dataclasses.replace(matching_span, start=span.start, end=span.end)
                    candidates.append(matching_span)
                    # Second path we choose based on reputation
                    candidate_weights.append(self.state.reputations.get_peer_reputation(matching_span.peer_id))
            
            if len(candidates) == 0:
                logger.info(f"Unable to find a match to replace span from {span.start}->{span.end}")
                new_path.append(span)
                continue
            
            logger.info(f"Found {len(candidates)} candidates to replace span from {span.start}->{span.end}")
            candidate_weights = np.array(candidate_weights) / np.sum(candidate_weights)
            chosen_candidate = np.random.choice(candidates, p=candidate_weights)
            new_path.append(chosen_candidate)
        
        return new_path

    def _make_sequence_with_min_latency(
        self, start_index: int, end_index: int, *, cache_tokens_needed: Optional[int]
    ) -> List[RemoteSpanInfo]:
        if start_index == end_index:
            return []

        with self.lock_changes:
            missing_blocks = [
                block_idx
                for block_idx in range(start_index, end_index)
                if not self.state.sequence_info.spans_containing_block[block_idx]
            ]
            if missing_blocks:
                raise MissingBlocksError(missing_blocks)
            server_infos = {
                span.peer_id: span.server_info
                for block_idx in range(start_index, end_index)
                for span in self.state.sequence_info.spans_containing_block[block_idx]
            }

            graph = self._build_inference_graph(start_index, end_index, cache_tokens_needed=cache_tokens_needed)

        path = dijkstar.find_path(graph, "start", "end")
        logger.debug(f"Path info: {path}")
        if start_index == 0 and end_index == len(self):
            logger.debug(f"Expected speed: {1 / path.total_cost:.1f} steps/sec")

        span_sequence = []
        for peer_id, block_idx in path.nodes[1:-1]:
            if not span_sequence or span_sequence[-1].peer_id != peer_id:
                span_sequence.append(RemoteSpanInfo(peer_id, block_idx, block_idx, server_infos[peer_id]))
            else:
                span_sequence[-1].end = block_idx

        # Remove empty spans that can appear if we don't force to go to the end of each server and network delay
        # don't follow triangle inequality (delay(A, B) + delay(B, C) < delay(A, C)) due to measurement errors
        span_sequence = [span for span in span_sequence if span.length > 0]

        return span_sequence

    def _build_inference_graph(
        self,
        start_index: int,
        end_index: int,
        *,
        cache_tokens_needed: Optional[int],
        overhead_delay: float = 0.018,  # Serialization overhead (empirically measured)
        default_inference_rps: float = 300,  # If inference RPS unknown
        alloc_delay: float = 10,  # If not enough cache left, we penalize the edge
    ) -> dijkstar.Graph:
        missing_blocks = [
            block_idx
            for block_idx in range(start_index, end_index)
            if not self.state.sequence_info.spans_containing_block[block_idx]
        ]
        if missing_blocks:
            raise MissingBlocksError(missing_blocks)

        client_server_rtts = self.ping_aggregator.to_dict()

        graph = dijkstar.Graph()

        # Clent -> server network delays
        for span in self.state.sequence_info.spans_containing_block[start_index]:
            delay = self._rtt_to_delay(client_server_rtts.get(span.peer_id))
            delay += overhead_delay
            if not self._has_cache_for(span, cache_tokens_needed):
                delay += alloc_delay
            graph.add_edge("start", (span.peer_id, start_index), delay)

        # Server -> client network delays
        for span in self.state.sequence_info.spans_containing_block[end_index - 1]:
            delay = self._rtt_to_delay(client_server_rtts.get(span.peer_id))
            graph.add_edge((span.peer_id, end_index), "end", delay)

        # Server -> server network delays
        for block_idx in range(start_index + 1, end_index):
            for cur_span in self.state.sequence_info.spans_containing_block[block_idx - 1]:
                if cur_span.end != block_idx:
                    # If we choose a server, we force to go to the end of it before switching to a new one
                    # to avoid O(N^2) graphs for N servers
                    continue

                for next_span in self.state.sequence_info.spans_containing_block[block_idx]:
                    rtt = None
                    if cur_span.server_info.next_pings is not None:
                        rtt = cur_span.server_info.next_pings.get(next_span.peer_id.to_base58())
                    delay = self._rtt_to_delay(rtt)
                    delay += overhead_delay
                    if not self._has_cache_for(next_span, cache_tokens_needed):
                        delay += alloc_delay
                    graph.add_edge((cur_span.peer_id, block_idx), (next_span.peer_id, block_idx), delay)

        # Compute delays
        for span in self.state.sequence_info.spans_by_priority:
            for block_idx in range(max(span.start, start_index), min(span.end, end_index)):
                inference_rps = span.server_info.inference_rps
                if inference_rps is None:
                    inference_rps = default_inference_rps
                graph.add_edge((span.peer_id, block_idx), (span.peer_id, block_idx + 1), 1.0 / inference_rps)

        return graph

    @staticmethod
    def _rtt_to_delay(
        rtt: float,
        *,
        default_delay: float = 0.15,  # If network delay unknown
        max_delay: float = 5,  # If unreachable, we don't want to discard the edge completely
    ) -> float:
        if rtt is None:
            return default_delay
        return min(rtt / 2, max_delay)

    @staticmethod
    def _has_cache_for(span: RemoteSpanInfo, cache_tokens_needed: Optional[int] = None) -> bool:
        if cache_tokens_needed is None or span.server_info.cache_tokens_left is None:
            return True

        # Here, `span` contains all blocks hosted by a server - but we won't necessarily run all of them through
        # this particular server in our path. It is difficult to estimate how many blocks we'll use at this stage,
        # so we assume that we'll use all of them (the worst case for the cache size) and get a pessimistic estimate.
        # This is okay since false positives are more costly than false negatives here.
        return cache_tokens_needed * 2 * span.length <= span.server_info.cache_tokens_left

    def _make_sequence_with_max_throughput(self, start_index: int, end_index: int) -> List[RemoteSpanInfo]:
        client_server_rtts = self.ping_aggregator.to_dict()

        span_sequence = []
        current_index = start_index
        while current_index < end_index:
            candidate_spans = self.state.sequence_info.spans_containing_block[current_index]
            if not candidate_spans:
                raise MissingBlocksError(current_index)

            # We choose longer servers to minimize the number of hops but leave some randomization
            # to distribute the load. We also exclude servers known to be unreachable.
            eps = 1e-6
            throughput_weights = np.array(
                [span.length if client_server_rtts.get(span.peer_id) != np.inf else eps for span in candidate_spans],
                dtype=np.float64,
            )
            throughput_weights /= throughput_weights.sum()

            reputation_weights = np.array(
                [self.state.reputations.get_peer_reputation(span.peer_id) for span in candidate_spans],
                dtype=np.float64
            )
            reputation_weights /= reputation_weights.sum()
            span_weights = ((1-self.state.reputation_weight) * throughput_weights) + (self.state.reputation_weight * reputation_weights)

            chosen_span = np.random.choice(candidate_spans, p=span_weights)

            assert chosen_span.start <= current_index < chosen_span.end
            span_sequence.append(dataclasses.replace(chosen_span, start=current_index))
            current_index = chosen_span.end
        return span_sequence

    def __getitem__(self, ix: Union[int, slice]) -> RemoteSequenceManager:
        """Get a RemoteSequenceManager for a sub-sequence of blocks"""
        assert isinstance(ix, (int, slice))
        if not isinstance(ix, slice):
            ix = slice(int(ix), int(ix) + 1, 1)
        return type(self)(self.config, self.block_uids[ix], dht=self.dht, state=self.state[ix])

    def update(self, *, wait: bool):
        """Run an asynchronous update in background as soon as possible"""
        self.ready.clear()
        self._thread.trigger.set()
        if wait:
            self.ready.wait()

    def _update(self):
        """Perform an immediate and synchronous refresh, may take time"""

        new_block_infos = get_remote_module_infos(
            self.dht, self.block_uids, active_adapter=self.config.active_adapter, latest=True
        )

        for block_info in new_block_infos:
            # Apply allow and block lists
            block_info.servers = {
                peer_id: server_info
                for peer_id, server_info in block_info.servers.items()
                if (self.allowed_servers is None or peer_id in self.allowed_servers)
                and (self.blocked_servers is None or peer_id not in self.blocked_servers)
            }

            # Remove temporarily banned peers, unless there are no peers left
            valid_servers = {
                peer_id: server_info
                for peer_id, server_info in block_info.servers.items()
                if peer_id not in self.state.banned_peers
            }
            if len(valid_servers) < len(block_info.servers):
                if valid_servers:
                    logger.debug(
                        f"Kept {len(valid_servers)} out of {len(block_info.servers)} servers holding {block_info.uid}"
                    )
                    block_info.servers = valid_servers
                else:
                    # If we blacklisted all servers, the error may actually be client-caused
                    logger.debug(f"All servers holding {block_info.uid} are blacklisted, ignoring blacklist")

        with self.lock_changes:
            self.state.sequence_info.update_(new_block_infos)

            first_servers = [span.peer_id for span in self.state.sequence_info.spans_containing_block[0]]
            middle_servers = [
                span.peer_id for spans in self.state.sequence_info.spans_containing_block[1:-1] for span in spans
            ]
            last_servers = [span.peer_id for span in self.state.sequence_info.spans_containing_block[-1]]

        pinged_servers = set(sample_up_to(first_servers, self.config.max_pinged))
        pinged_servers = set(sample_up_to(middle_servers, self.config.max_pinged))
        pinged_servers |= set(sample_up_to(last_servers, self.config.max_pinged))
        self.ping_aggregator.ping(list(pinged_servers), wait_timeout=self.config.ping_timeout)

        self.ready.set()

    def on_request_failure(self, peer_id: Optional[PeerID], other_peer_id : Optional[PeerID]):
        """remove a given peer from the routing table. If the routing is no longer possible, trigger an update"""
        logger.info(f"On request failure: {peer_id}")
        if peer_id is not None:
            if other_peer_id is not None:
                if peer_id in self.state.trusted_peers:
                    self.state.banned_peers.register_failure(other_peer_id)
                elif other_peer_id in self.state.trusted_peers:
                    self.state.banned_peers.register_failure(peer_id)
                else:
                    logger.debug(f"Peer {peer_id} disagreed, harming reputation")
                    self.state.reputations.register_disagreement(peer_id)
                    self.state.reputations.register_disagreement(other_peer_id)
            else:
                logger.debug(f"Peer {peer_id} did not respond, banning it temporarily")
                self.state.banned_peers.register_failure(peer_id)
                
        with self.lock_changes:
            should_update = False
            for info in self.state.sequence_info.block_infos:
                info.servers.pop(peer_id, None)
                if not info.servers:
                    should_update = True
            if should_update:
                self.ready.clear()
                self.update(wait=False)

    def on_request_success(self, peer_id: PeerID, registerAgreement : bool = True):
        """if peer has a failure streak, clear that streak"""
        if registerAgreement:
            logger.debug(f"Peer {peer_id} agreed, improving reputation")
            self.state.reputations.register_agreement(peer_id)
        
        self.state.banned_peers.register_success(peer_id)

    def __len__(self):
        return len(self.block_uids)

    @property
    def is_alive(self):
        return self._thread.is_alive

    @property
    def ready(self) -> threading.Event:
        return self._thread.ready

    @property
    def block_uids(self):
        return self.state.sequence_info.block_uids

    @property
    def rpc_info(self):
        """Return the rpc_info queried from one of the servers that hold the first block"""
        if self.state.rpc_info is not None:
            return self.state.rpc_info

        with self._thread_start_lock:
            if not self.is_alive():
                self._thread.start()

        for attempt_no in itertools.count():
            peer_id = None
            try:
                if not self.ready.is_set():
                    self.update(wait=True)

                active_servers = [
                    peer_id
                    for peer_id, server in self.state.sequence_info.block_infos[0].servers.items()
                    if server.state == ServerState.ONLINE
                ]
                if not active_servers:
                    raise MissingBlocksError(0)
                peer_id = random.choice(active_servers)

                stub = TransformerConnectionHandler.get_stub(self.state.p2p, peer_id)
                outputs = RemoteExpertWorker.run_coroutine(
                    stub.rpc_info(runtime_pb2.ExpertUID(uid=self.block_uids[0]), timeout=self.config.request_timeout)
                )
                self.state.rpc_info = MSGPackSerializer.loads(outputs.serialized_info)
                self.on_request_success(peer_id)
                break
            except Exception as e:
                self.on_request_failure(peer_id)
                if attempt_no + 1 == self.config.max_retries:
                    raise
                delay = self.get_retry_delay(attempt_no)
                logger.warning(
                    f"Caught exception when gathering information from peer {peer_id} "
                    f"(retry in {delay:.0f} sec): {repr(e)}"
                )
                maybe_log_traceback(e)
                time.sleep(delay)

        return self.state.rpc_info

    def get_retry_delay(self, attempt_no: int) -> float:
        if attempt_no == 0:
            return 0
        return min(self.config.min_backoff * 2 ** (attempt_no - 1), self.config.max_backoff)

    def get_request_metadata(
        self, protocol: str, args_structure: Any = None, *args, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        :param protocol: one of "rpc_forward", "rpc_backward" or "rpc_inference"
        :param args_structure: the structure of flattened tensors from pack_args_kwargs in petals.utils.packaging
        :param args: request-specific inputs, typically block uids and input tensors
        :param kwargs: additional request context, such as remote peer ID
        :returns: msgpack-serialized metadata dict that will be passed alongside a given request
        """
        return dict(
            points=self.policy.get_points(protocol, *args, **kwargs),
            active_adapter=self.config.active_adapter,
            args_structure=args_structure,
        )

    def shutdown(self):
        self._thread.shutdown()


class _SequenceManagerUpdateThread(threading.Thread):
    def __init__(self, update_period: float, ref_update_manager: WeakMethod):
        super().__init__(daemon=True)
        self.ref_update_manager = ref_update_manager
        self.ready = threading.Event()
        self.trigger = threading.Event()
        self.update_period = update_period
        self.should_shutdown = False

    def run(self) -> None:
        while not self.should_shutdown:
            update_manager = self.ref_update_manager()
            if update_manager is None:
                logger.debug(f"{self.__class__.__name__} exited because the sequence manager no longer exists")
                break

            try:
                self.trigger.clear()
                update_manager()
            except Exception as e:
                logger.exception(e)
            finally:
                del update_manager

            self.trigger.wait(self.update_period)

        logger.debug(f"{self.__class__.__name__} thread exited")

    def shutdown(self, timeout: Optional[float] = None):
        self.should_shutdown = True
        self.trigger.set()
        if self.is_alive():
            self.join(timeout)

    def __del__(self):
        self.shutdown()


def maybe_log_traceback(exc: Exception):
    traceback_level = logging.DEBUG if str(exc) or isinstance(exc, asyncio.TimeoutError) else logging.WARNING
    logger.log(traceback_level, "See detailed traceback below:", exc_info=True)


class MissingBlocksError(RuntimeError):
    def __init__(self, block_indices: Union[int, Sequence[int]]):
        super().__init__(
            f"No servers holding blocks {block_indices} are online. "
            f"You can check the public swarm's state at https://health.petals.dev "
            f"If there are not enough servers, please connect your GPU: "
            f"https://github.com/bigscience-workshop/petals#connect-your-gpu-and-increase-petals-capacity "
        )
