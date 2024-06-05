from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from typing import AsyncIterator, List, Optional, Tuple
import threading

import torch
from hivemind import MSGPackSerializer, anext, deserialize_torch_tensor, get_logger, serialize_torch_tensor
from hivemind.moe.client.remote_expert_worker import RemoteExpertWorker
from hivemind.p2p import P2P
from hivemind.proto import runtime_pb2
from hivemind.utils.tensor_descr import BatchTensorDescriptor

from petals.client.config import ClientConfig
from petals.client.routing import RemoteSequenceManager, maybe_log_traceback
from petals.data_structures import CHAIN_DELIMITER, ModuleUID, RemoteSpanInfo, RPCInfo
from petals.server.handler import TransformerConnectionHandler
from petals.utils.misc import DUMMY, DUMMY_INT64, is_dummy
from petals.utils.packaging import pack_args_kwargs

logger = get_logger(__name__)


class _ServerInferenceSession:
    """
    An interface to a single multi-step *inference* session for a a set of blocks on a specific server.

    :note: This class is *not* fault-tolerant out of the box.
    """

    def __init__(
        self,
        config: ClientConfig,
        span: RemoteSpanInfo,
        uid: ModuleUID,
        rpc_info: RPCInfo,
        inputs_queue: asyncio.Queue,
        outputs_aiter: AsyncIterator,
        *,
        max_length: int,
        **metadata,
    ):
        self.config = config
        self.span, self.uid, self.rpc_info = span, uid, rpc_info
        self.num_blocks = uid.count(CHAIN_DELIMITER) + 1
        self._inputs_queue: asyncio.Queue[runtime_pb2.ExpertRequest] = inputs_queue
        self._outputs_stream: AsyncIterator[runtime_pb2.ExpertResponse] = outputs_aiter
        self.session_id = str(uuid.uuid4())
        self.session_metadata = dict(max_length=max_length, **metadata)
        self.stepped = False
        self.closed = False

        self._position = 0
        self.history = None  # Used in case of server failures to regenerate attention caches on new servers
        self.next_session = None

    @classmethod
    async def create(
        cls,
        config: ClientConfig,
        p2p: P2P,
        span: RemoteSpanInfo,
        uid: ModuleUID,
        rpc_info: RPCInfo,
        **metadata,
    ) -> _ServerInferenceSession:
        """Create a new session for a given remote module. This code is meant to be run inside RemoteExpertWorker"""
        stub = TransformerConnectionHandler.get_stub(p2p, span.peer_id)
        inputs_queue = asyncio.Queue()
        outputs_stream = await asyncio.wait_for(
            stub.rpc_inference(cls._read_inputs_from_queue(inputs_queue)),
            config.connect_timeout,
        )
        return cls(config, span, uid, rpc_info, inputs_queue, outputs_stream, **metadata)

    @staticmethod
    async def _read_inputs_from_queue(queue: asyncio.Queue, input_timeout: Optional[float] = None) -> AsyncIterator:
        while True:
            next_input_message = await asyncio.wait_for(queue.get(), input_timeout)
            yield next_input_message
            if not next_input_message.uid and not next_input_message.tensors:
                break  # this message means "done sending"

    def step(
        self, inputs: torch.Tensor, prompts: torch.Tensor, hypo_ids: torch.LongTensor, *, step_id: str
    ) -> torch.Tensor:
        """
        Inference step: send a chunk of input tensors and receive a chunk of outputs
        :prompts: optional DEEP prompts, added to a prefix of each layer's outputs,
          if specified, deep prompts should have shape [num_layers, batch_size, prefix_len, hid_size]
        """
        if self.closed:
            raise Exception("Session is closed, cannot perform step")

        n_input_tokens = inputs.shape[1]
        if self.history is None:
            self.history = inputs
        elif self.history.shape[1] == self._position:
            self.history = torch.cat([self.history, inputs[:, -n_input_tokens:]], dim=1)
        assert self.history.shape[1] == self._position + n_input_tokens, (
            f"Broken input cache: span={self.span} shape={self.history.shape} "
            f"position={self._position} n_input_tokens={n_input_tokens}"
        )

        if not self.stepped:
            inputs = self.history  # Pass full inputs including prefix
        else:
            inputs = inputs[:, -n_input_tokens:]  # No need to pass prefix further

        # serialize inputs and put them into the queue
        input_tensors, args_structure = pack_args_kwargs(inputs, prompts, hypo_ids)

        request_metadata = dict(session_id=self.session_id, step_id=step_id)
        if not self.stepped:
            request_metadata.update(self.session_metadata)
        elif self.config.use_server_to_server:
            next_servers = self._collect_next_servers()
            if next_servers:
                request_metadata["next_servers"] = next_servers

        request_metadata["args_structure"] = args_structure

        # TODO: make possible to use different compression method for different tensors
        server_side_inference_schema, kwargs_schema = self.rpc_info["inference_schema"]
        compression = server_side_inference_schema[0].compression
        inference_schema = tuple(BatchTensorDescriptor.from_tensor(arg, compression) for arg in input_tensors)

        # TODO: create more explicit way to check servers schema and client's structure
        assert len(input_tensors) >= len(
            server_side_inference_schema
        ), "Hidden_state, prompts and hypo_ids tensors are necessary for an inference step"

        outputs_serialized = RemoteExpertWorker.run_coroutine(
            self._step(
                runtime_pb2.ExpertRequest(
                    uid=self.uid,
                    tensors=[
                        serialize_torch_tensor(tensor.to(proto.dtype), proto.compression)
                        for tensor, proto in zip(input_tensors, inference_schema)
                    ],
                    metadata=MSGPackSerializer.dumps(request_metadata),
                )
            )
        )
        outputs = list(map(deserialize_torch_tensor, outputs_serialized.tensors))
        assert (
            outputs[0].shape == inputs.shape
        ), f"output activation shape is different from input shape: {outputs[0].shape} != {inputs.shape}"

        self._position += n_input_tokens

        return outputs[0]

    def _collect_next_servers(self) -> List[Tuple[str, str, int, int]]:
        next_servers = []
        session = self.next_session
        while session is not None and session.stepped:
            next_servers.append(
                (session.span.peer_id.to_base58(), session.session_id, session.span.start, session.span.end)
            )
            session = session.next_session
        return next_servers

    async def _step(self, inputs_serialized: runtime_pb2.ExpertRequest) -> runtime_pb2.ExpertResponse:
        """Inference step on serialized data. This code is meant to be run inside RemoteExpertWorker"""
        await self._inputs_queue.put(inputs_serialized)
        self.stepped = True
        return await asyncio.wait_for(anext(self._outputs_stream), self.config.request_timeout)

    def close(self):
        """Finish a given inference session, close the underlying connection"""
        if self._outputs_stream is None:
            return  # already closed
        RemoteExpertWorker.run_coroutine(self._aclose_stream())
        self._outputs_stream = self._inputs_queue = None
        self.closed = True

    async def _aclose_stream(self):
        """Close the inference session. This code is meant to be run inside RemoteExpertWorker"""
        if self._outputs_stream is None:
            return  # already closed
        if self.stepped:
            await self._inputs_queue.put(runtime_pb2.ExpertRequest())  # empty request will trigger end of session
            try:
                await anext(self._outputs_stream)
            except StopAsyncIteration:
                pass

    def __del__(self):
        self.close()

    def __enter__(self):
        assert not self.closed
        return self

    def __exit__(self, *exc_details):
        self.close()


class InferenceSession:
    """
    An interface to a multi-step *inference* session for a sequence of remote transformer blocks
    """

    def __init__(self, sequence_manager: RemoteSequenceManager, max_length: int):
        self._sequence_manager = sequence_manager
        self._closed = False
        self._server_sessions = []
        self._second_server_sessions = []
        self._position = 0
        self._max_length = max_length
        self.output_ids = None
        self.past_key_values = None

    @property
    def num_blocks(self) -> int:
        return len(self._sequence_manager)

    @property
    def position(self) -> int:
        return self._position

    def _enter_server_sessions(self, chosen_spans: List[RemoteSpanInfo]) -> List[_ServerInferenceSession]:
        server_sessions = []
        try:
            for span in chosen_spans:
                span_uids = CHAIN_DELIMITER.join(self._sequence_manager.block_uids[span.start : span.end])
                metadata = self._sequence_manager.get_request_metadata("rpc_inference", span_uids, peer_id=span.peer_id)
                session = RemoteExpertWorker.run_coroutine(
                    _ServerInferenceSession.create(
                        self._sequence_manager.config,
                        self._sequence_manager.state.p2p,
                        span,
                        span_uids,
                        rpc_info=self._sequence_manager.rpc_info,
                        max_length=self._max_length,
                        **metadata,
                    )
                )
                server_sessions.append(session)
                session.__enter__()
            return server_sessions
        except:
            self._exit_server_sessions(server_sessions)
            raise

    def _exit_server_sessions(self, server_sessions: List[_ServerInferenceSession]) -> None:
        for session in reversed(server_sessions):
            try:
                session.__exit__(None, None, None)
            except Exception:
                logger.debug("Caught exception while closing connection to server:", exc_info=True)

    def __enter__(self) -> "InferenceSession":
        assert not self._closed and not self._server_sessions
        return self

    def step(
        self, inputs: torch.Tensor, prompts: Optional[torch.Tensor] = None, hypo_ids: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        assert not self._closed
        if torch.is_grad_enabled():
            logger.warning("Running inference session with grad enabled. Gradients will *not* be propagated correctly.")

        if prompts is None or is_dummy(prompts):
            prompts = DUMMY
        else:
            assert prompts.ndim == 4, "deep prompts should have shape [num_blocks, batch_size, prefix_len, hid_size]"
            assert prompts.shape[0] == self.num_blocks
            assert prompts.shape[1] in (inputs.shape[0], 1)
            assert prompts.shape[2] <= inputs.shape[1]
            assert prompts.shape[3] == inputs.shape[2]

        if hypo_ids is None or is_dummy(hypo_ids):
            hypo_ids = DUMMY_INT64
        else:
            assert len(hypo_ids) == len(inputs)
            assert hypo_ids.dtype == torch.int64

        inputs_device = inputs.device
        inputs_dtype = inputs.dtype
        inputs = inputs.cpu()
        prompts = prompts.cpu()
        hypo_ids = hypo_ids.cpu()
        step_id = str(uuid.uuid4())

        n_input_tokens = inputs.shape[1]
        if self._position + n_input_tokens > self._max_length:
            raise ValueError(
                f"Maximum length exceeded: prefix {self._position} + current {n_input_tokens} exceeds pre-allocated maximum {self._max_length}"
            )

        server_idx = 0
        block_idx = 0
        while block_idx < self.num_blocks:
            for attempt_no in itertools.count():
                logger.debug(f"Inference: block {block_idx}, attempt {attempt_no}")
                server_session = None
                disagreement = False
                try:
                    if not self._server_sessions or attempt_no >= 1:
                        self._update_sequence(server_idx, block_idx, attempt_no)

                    server_session = self._server_sessions[server_idx]
                    second_server_session = self._second_server_sessions[server_idx]
                    run_second_inference = server_session.span.peer_id != second_server_session.span.peer_id
                    if run_second_inference:
                        second_inputs = inputs.detach().clone()
                    logger.info(f"Running inference for {block_idx} via {server_session.span.peer_id} and {second_server_session.span.peer_id}")
                    def f():
                        nonlocal inputs
                        inputs = server_session.step(
                            inputs, prompts[server_session.span.start : server_session.span.end], hypo_ids, step_id=step_id
                        )
                    thread = threading.Thread(target=f)
                    thread.start()
                    if run_second_inference:
                        logger.info(f"Running inference via second server")
                        second_inputs = second_server_session.step(
                            second_inputs, prompts[second_server_session.span.start : second_server_session.span.end], hypo_ids, step_id=step_id
                        )
                    thread.join()
                    if not run_second_inference: 
                        logger.info(f"Serving from the same server, skipping second server inference")
                        second_inputs = inputs
                    
                    if run_second_inference and not torch.allclose(inputs , second_inputs):
                        logger.warning(f"Outputs from two different paths are different")
                        disagreement = True
                        raise ValueError("Outputs from two different paths are different")

                    server_idx += 1
                    block_idx = server_session.span.end
                    self._sequence_manager.on_request_success(server_session.span.peer_id, run_second_inference)
                    if run_second_inference:
                        self._sequence_manager.on_request_success(second_server_session.span.peer_id, run_second_inference)
                    break
                except Exception as e:
                    self._sequence_manager.on_request_failure(
                        server_session.span.peer_id if server_session is not None else None, disagreement
                    )
                    if server_session != second_server_session:
                        self._sequence_manager.on_request_failure(
                            second_server_session.span.peer_id if second_server_session is not None else None, disagreement
                        )
                    if attempt_no + 1 == self._sequence_manager.config.max_retries:
                        raise
                    delay = self._sequence_manager.get_retry_delay(attempt_no)
                    logger.warning(
                        f"Caught exception when running inference via {server_session.span if server_session is not None else None} "
                        f"(retry in {delay:.0f} sec): {repr(e)}"
                    )
                    maybe_log_traceback(e)
                    time.sleep(delay)

        self._position += n_input_tokens
        outputs = inputs[:, -n_input_tokens:]
        outputs = outputs.to(device=inputs_device, dtype=inputs_dtype)
        return outputs

    def _update_sequence(self, server_idx: int, block_idx: int, attempt_no: int) -> int:
        # If there is a failed server session, this code closes it
        self._exit_server_sessions(self._server_sessions[server_idx : server_idx + 1])
        self._exit_server_sessions(self._second_server_sessions[server_idx : server_idx + 1])

        n_prev_spans = len(self._server_sessions)
        second_n_prev_spans = len(self._second_server_sessions)
        update_end = self._server_sessions[server_idx].span.end if server_idx < n_prev_spans else self.num_blocks
        second_update_end = self._second_server_sessions[server_idx].span.end if server_idx < second_n_prev_spans else self.num_blocks
        if attempt_no >= 1:
            logger.debug(
                f"Due to a server failure, remote attention caches "
                f"from block {block_idx} to {update_end} will be regenerated"
            )

        updated_spans = self._sequence_manager.make_sequence(
            block_idx, update_end, mode="min_latency", cache_tokens_needed=self._max_length
        )
        second_updated_spans = self._sequence_manager.make_second_replacement_sequence(block_idx, update_end,
                                                                              mode="max_throughput",
                                                                              confidence=0.5,
                                                                              currentPath=updated_spans)
        new_route_repr = " => ".join(
                [f"{span.start}:{span.end} via …{str(span.peer_id)[-6:]}" for span in second_updated_spans])
        logger.info(f"Gordon: New route: {new_route_repr}")         
        
        if len(updated_spans) != len(second_updated_spans):
            # Simplifying assumption. Should be able to relax this in the future
            raise ValueError("The number of servers in the two paths are different")
        # make_sequence() could return a longer sequence
        updated_spans[-1].end = min(updated_spans[-1].end, update_end)
        second_updated_spans[-1].end = min(second_updated_spans[-1].end, second_update_end)
        updated_sessions = self._enter_server_sessions(updated_spans)
        second_updated_sessions = updated_sessions[:] # Copy by value and not by reference
        for i, (span, second_span) in enumerate(zip(updated_spans, second_updated_spans)):
            logger.info(f"Span {i}: {span.peer_id} vs {second_span.peer_id}")
            if span.peer_id != second_span.peer_id:
                logger.info("Different servers found")
                second_updated_sessions[i] = self._enter_server_sessions([second_span])[0]
        logger.debug(f"Found path from block {block_idx} to {update_end} via {len(updated_spans)} servers")
        logger.debug(f"Found second_path from block {block_idx} to {second_update_end} via {len(second_updated_spans)} servers")

        # If there is a failed span, this code replaces it, otherwise it just adds new ones
        if server_idx < n_prev_spans:
            updated_sessions[0].history = self._server_sessions[server_idx].history
        self._server_sessions[server_idx : server_idx + 1] = updated_sessions
        if server_idx < second_n_prev_spans:
            second_updated_sessions[0].history = self._second_server_sessions[server_idx].history
        self._second_server_sessions[server_idx : server_idx + 1] = second_updated_sessions

        # Update links to the next server session for direct server-to-server communication via rpc_push()
        for i in range(max(server_idx - 1, 0), min(server_idx + len(updated_spans), len(self._server_sessions) - 1)):
            self._server_sessions[i].next_session = self._server_sessions[i + 1]
        for i in range(max(server_idx - 1, 0), min(server_idx + len(second_updated_spans), len(self._second_server_sessions) - 1)):
            self._second_server_sessions[i].next_session = self._second_server_sessions[i + 1]

    def close(self, *exc_details):
        """Finish a given inference session, close the underlying connection"""
        if not self._closed:
            self._exit_server_sessions(self._server_sessions)
            self._exit_server_sessions(self._second_server_sessions)
            self._server_sessions.clear()
            self._second_server_sessions.clear()
            self._closed = True

    def __exit__(self, *exc_details):
        self.close(*exc_details)

    def __del__(self):
        self.close()

    @property
    def last_token_id(self) -> Optional[torch.Tensor]:  # Backward compatibility with Petals < 2.1.0
        return self.output_ids[:, -1:] if self.output_ids is not None else None

    @last_token_id.setter
    def last_token_id(self, value: torch.Tensor):  # Backward compatibility with Petals < 2.1.0
        if self.output_ids is None:
            raise RuntimeError("Can't override `last_token_id` since the session has not stepped yet")
        self.output_ids[:, -1:] = value
