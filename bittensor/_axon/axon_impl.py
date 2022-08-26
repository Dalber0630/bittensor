""" Implementation of Axon, services Forward and Backward requests from other neurons.
"""
# The MIT License (MIT)
# Copyright © 2021 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated 
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation 
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, 
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of 
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL 
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION 
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER 
# DEALINGS IN THE SOFTWARE.

import time as clock
from typing import List, Tuple, Callable

import torch
import grpc
import sys
from loguru import logger
import concurrent

import bittensor

# Prometheus
from prometheus_client import Counter
from prometheus_client import Histogram
from prometheus_client import Enum

logger = logger.opt(colors=True)

class Axon( bittensor.grpc.BittensorServicer ):
    r""" Services Forward and Backward requests from other neurons.
    """
    def __init__( 
        self, 
        wallet: 'bittensor.wallet',
        ip: str,
        port: int,
        server: 'grpc._Server',
        forward: 'Callable',
        backward: 'Callable',
        synapses: dict,
        synapse_checks: 'Callable',
        priority:  'Callable' = None,
        priority_threadpool: 'bittensor.prioritythreadpool' = None,
        forward_timeout: int = None,
        backward_timeout: int = None,
        prometheus_level: str,
    ):
        r""" Initializes a new Axon tensor processing endpoint.
            
            Args:
                config (:obj:`bittensor.Config`, `required`): 
                    bittensor.axon.config()
                wallet (:obj:`bittensor.wallet`, `required`):
                    bittensor wallet with hotkey and coldkeypub.
                server (:obj:`grpc._Server`, `required`):
                    Grpc server endpoint.
                forward (:obj:list of `callable`, `optional`):
                    list of functions which is called on forward requests.
                backward (:obj:list of `callable`, `optional`):
                    list of functions which is called on backward requests.
                priority (:obj:`callable`, `optional`):
                    function to assign priority on requests.
                priority_threadpool (:obj:`bittensor.prioritythreadpool`, `optional`):
                    bittensor priority_threadpool.     
                prometheus_level (:obj:`str`, `required`):
                    Prometheus logging level.             
        """
        # -- Object vars.
        self.ip = ip
        self.port = port
        self.wallet = wallet
        self.server = server
        self.forward_callback = forward if forward != None else self.default_forward_callback
        self.backward_callback = backward if backward != None else self.default_backward_callback
        self.forward_timeout = forward_timeout
        self.backward_timeout = backward_timeout
        self.synapse_callbacks = synapses
        self.synapse_checks = synapse_checks
        self.prometheus_level = prometheus_level

        self.optimizer_step = None
        self.started = None

        # -- Priority 
        self.priority = priority 
        self.priority_threadpool= priority_threadpool

        # -- Prometheus
        # We are running over various suffix values in event that there are multiple axons in the same process.
        # The first axon is created with a null suffix. Values are ordered like so: axon_is_started, axon_is_started_1, axon_is_started_2 etc...
        if self.prometheus_level != bittensor.prometheus.level.OFF.name:
            suffix = ""; idx = 1
            while True:
                try:
                    self.is_started = Enum('axon_is_started{}'.format(suffix), 'is_started', states=['stopped', 'started'])
                    self.total_forward = Counter('axon_total_forward{}'.format(suffix), 'total_forward')
                    self.total_backward = Counter('axon_total_backward{}'.format(suffix), 'total_backward')
                    self.forward_latency = Histogram('axon_forward_latency{}'.format(suffix), 'forward_latency', buckets=list(range(0,2*bittensor.__blocktime__,1)))
                    self.backward_latency = Histogram('axon_backward_latency{}'.format(suffix), 'backward_latency', buckets=list(range(0,2*bittensor.__blocktime__,1))) 
                    self.forward_synapses = Counter('axon_forward_synapses{}'.format(suffix), 'forward_synapses', ["synapse"])
                    self.backward_synapses = Counter('axon_backward_synapses{}'.format(suffix), 'backward_synapses', ["synapse"])
                    self.forward_codes = Counter('axon_forward_codes{}'.format(suffix), 'forward_codes', ["code"])
                    self.backward_codes = Counter('axon_backward_codes{}'.format(suffix), 'backward_codes', ["code"])
                    self.forward_hotkeys = Counter('axon_forward_hotkeys{}'.format(suffix), 'forward_hotkeys', ["hotkey"])
                    self.backward_hotkeys = Counter('axon_backward_hotkeys{}'.format(suffix), 'backward_hotkeys', ["hotkey"])
                    self.forward_bytes = Counter('axon_forward_bytes{}'.format(suffix), 'forward_bytes', ["hotkey"])
                    self.backward_bytes = Counter('axon_backward_bytes{}'.format(suffix), 'backward_bytes', ["hotkey"])
                except ValueError: 
                    suffix = "_{}".format(str(idx)); idx+=1
                    bittensor.__console__.print('Sending next axon prometheus args to suffix: {}'.format(suffix))
                    continue
                break

    def __str__(self) -> str:
        return "Axon({}, {}, {}, {})".format( self.ip, self.port, self.wallet.hotkey.ss58_address, "started" if self.started else "stopped")

    def __repr__(self) -> str:
        return self.__str__()

    def Forward(self, request: bittensor.proto.TensorMessage, context: grpc.ServicerContext) -> bittensor.proto.TensorMessage:
        r""" The function called by remote GRPC Forward requests from other neurons.
            Forward is equivalent to a 'forward' pass through a neural network.
            After checking request validity, this function passes the request to the nucleus for processing.
            See :obj:`bittensor.proto.ReturnCode` for all possible return codes.
            
            Args:
                request (:obj:`bittensor.proto`, `required`): 
                    Tensor request proto.
                context (:obj:`grpc.ServicerContext`, `required`): 
                    grpc server context.
            
            Returns:
                response (bittensor.proto.TensorMessage): 
                    proto response carring the nucleus forward output or None under failure.
        """
        forward_response_tensors, code, synapses = self._forward( request )
        response = bittensor.proto.TensorMessage(
            version = bittensor.__version_as_int__, 
            hotkey = self.wallet.hotkey.ss58_address, 
            return_code = code,
            tensors = forward_response_tensors if forward_response_tensors is not None else [],
            requires_grad = request.requires_grad,
            synapses = synapses,
        )
        return response

    def Backward( self, request: bittensor.proto.TensorMessage, context: grpc.ServicerContext ) -> bittensor.proto.TensorMessage:
        r""" The function called by remote GRPC Backward requests from other neurons.
            Backward is equivalent to a 'backward' gradient descent pass through a neural network.
            After checking request validity, passes the request to the nucleus for processing.
            See :obj:`bittensor.proto.ReturnCode` for all possible return codes.
            
            Args:
                request (:obj:`bittensor.proto`, `required`): 
                    Tensor request proto.
                context (:obj:`grpc.ServicerContext`, `required`): 
                    grpc server context.
            
            Returns:
                response (:obj:`bittensor.proto.TensorMessage`): 
                    proto response carring the nucleus backward output or None under failure.
        """
        backward_response_tensors, code, synapses = self._backward( request )
        response = bittensor.proto.TensorMessage(
            version = bittensor.__version_as_int__, 
            hotkey = self.wallet.hotkey.ss58_address, 
            return_code = code,
            tensors = backward_response_tensors,
            requires_grad = request.requires_grad,
            synapses = synapses
        )
        return response

    def _forward(self, request):
        r""" Performs validity checks on the grpc request before passing the tensors to the forward queue.
            Returns the outputs and synapses from the backend forward call.
            
            Args:
                request (:obj:`bittensor.proto`, `required`): 
                    Tensor request proto.
            Returns:
                response (:obj:`bittensor.proto.Tensor, `required`): 
                    serialized tensor response from the nucleus call or None.
                code (:obj:`bittensor.proto.ReturnCode`, `required`):
                    Code from the call. This specifies if the overall function call was a success. 
                    This is separate from the synapse returns codes which relate to the individual synapse call. 
                synapses (:obj:`List[ 'bittensor.proto.Synapse' ]` of shape :obj:`(num_synapses)`, `required`):
                    Synapse wire protos with return codes from forward request.
        """
        # ===================================================================
        # ==== First deserialize synapse wire protos to instance objects ====        
        # ===================================================================
        synapses: List['bittensor.Synapse'] = []
        for synapse_wire_proto in request.synapses:
            synapses.append( bittensor.synapse.deserialize( synapse_wire_proto ) )


        # ===================================
        # ==== Init params from synapses ====        
        # ===================================
        # These items are filled through the call and the function returns 
        # when all codes are non-success or the function finishes completely.
        synapse_messages = [ "Success" for _ in synapses ]
        synapse_codes = [ bittensor.proto.ReturnCode.Success for _ in synapses ]
        synapse_inputs = [ None for _ in synapses ]
        synapse_responses = [ synapse.empty() for synapse in synapses ] # We fill nones for non success.
        synapse_is_response = [ False for _ in synapses ]
        synapse_call_times = [ 0 for _ in synapses ]
        start_time = clock.time()

        # ==================================================================
        # ==== Function which returns true if all codes are non success ====
        # ==================================================================
        def check_if_should_return() -> bool:
            for code in synapse_codes:
                if code == bittensor.proto.ReturnCode.Success:
                    return False
            return True


        # ==============================================================
        # ==== Function which prints all log statements per synapse ====
        # ==============================================================
        def finalize_codes_stats_and_logs():
            if self.prometheus:
                self.total_forward.inc()
                self.forward_latency.observe( clock.time() - start_time )
                self.forward_hotkeys.labels( request.hotkey ).inc()
                self.forward_bytes.labels( request.hotkey ).inc( sys.getsizeof( request.hotkey) )
            for index, synapse in enumerate( synapses ):
                if self.prometheus:
                    self.forward_synapses.labels( str(synapse) ).inc()
                    self.forward_codes.labels( str(synapse_codes[ index ]) ).inc()
                request.synapses [ index ].return_code = synapse_codes[ index ] # Set synapse wire proto codes.
                request.synapses [ index ].message = synapse_messages[ index ] # Set synapse wire proto message
                bittensor.logging.rpc_log ( 
                    axon = True, 
                    forward = True, 
                    is_response = synapse_is_response [index], 
                    code = synapse_codes[ index ], 
                    call_time = synapse_call_times[ index ], 
                    pubkey = request.hotkey, 
                    inputs = synapse_inputs [index] , 
                    outputs = None if synapse_responses[index] == None else list( synapse_responses[index].shape ), 
                    message = synapse_messages[ index ],
                    synapse = synapse.synapse_type
                )

        # ======================================
        # ==== Check Empty request ====
        # ======================================
        if len(request.tensors) == 0:
            code = bittensor.proto.ReturnCode.EmptyRequest
            message = "Forward request contains {} tensors, expected 1 tensor in the forward call".format(len(request.tensors))
            call_time = clock.time() - start_time
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], code, request.synapses

        
        # ======================================
        # ==== Check request length ====
        # ======================================
        if len( request.tensors ) != len( synapses ):
            # Not enough responses per request.
            code = bittensor.proto.ReturnCode.RequestShapeException
            call_time = clock.time() - start_time
            message = "Request length doesn't match synape length."
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.RequestShapeException, request.synapses


        # ===================================
        # ==== Deserialize/Check inputs ====
        # ===================================
        deserialized_forward_tensors = [ None for _ in synapses]
        for index, synapse in enumerate( synapses ):
            try:
                deserialized_forward_tensors [index] = synapse.deserialize_forward_request_tensor ( request.tensors [index] )

            except ValueError as e:
                synapse_codes [index] = bittensor.proto.ReturnCode.RequestShapeException
                synapse_call_times [index] = clock.time() - start_time
                synapse_messages [index] = 'Input shape exception with error:{}'.format(str(e))

            except Exception as e:
                synapse_codes [index] = bittensor.proto.ReturnCode.RequestDeserializationException
                synapse_call_times [index] = clock.time() - start_time
                synapse_messages [index] = 'Input deserialization exception with error:{}'.format(str(e))
        # Check if the call can stop here.
        if check_if_should_return():
            finalize_codes_stats_and_logs()
            return [], synapse_codes[0] , request.synapses


        # ===================================
        # ==== Make forward calls. =========
        # ===================================
        try:
            finalize_codes_stats_and_logs()
            if self.priority != None:
                priority = self.priority( request.hotkey, inputs_x = deserialized_forward_tensors, request_type = bittensor.proto.RequestType.FORWARD )
                future = self.priority_threadpool.submit (
                    self.forward_callback,
                    inputs_x = deserialized_forward_tensors, 
                    synapses = synapses,
                    priority = priority,
                    hotkey= request.hotkey
                )
                forward_response_tensors, forward_codes, forward_messages = future.result( timeout= self.forward_timeout )
            else:
                
                forward_response_tensors, forward_codes, forward_messages = self.forward_callback(
                    inputs_x = deserialized_forward_tensors,
                    synapses = synapses,
                    hotkey= request.hotkey
                )
            synapse_is_response = [ True for _ in synapses ]
            # ========================================
            # ==== Fill codes from forward calls ====
            # ========================================
            for index, synapse in enumerate(synapses):
                synapse_codes [ index ] = forward_codes [ index ]
                synapse_messages [index] = forward_messages [ index ]
        # ========================================
        # ==== Catch forward request timeouts ====
        # ========================================
        except concurrent.futures.TimeoutError:
            if self.priority != None:
                future.cancel()
            code = bittensor.proto.ReturnCode.Timeout
            call_time = clock.time() - start_time
            message = "Request reached timeout"
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.Timeout, request.synapses

        # ==================================
        # ==== Catch unknown exceptions ====
        # ==================================
        except Exception as e:
            code = bittensor.proto.ReturnCode.UnknownException
            call_time = clock.time() - start_time
            message = str ( e )
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.UnknownException, request.synapses

        # =================================================
        # ==== Encode/serialize responses and synapses ====
        # ==================================================
        response_synapses = []
        for index, synapse in enumerate( synapses ):
            try:
                if synapse_codes[index] == bittensor.proto.ReturnCode.Success:
                    synapse_responses [ index ] = synapse.serialize_forward_response_tensor( deserialized_forward_tensors[ index ], forward_response_tensors [ index ] )
                else:
                    synapse_responses [ index ] = synapse.empty()

            except ValueError as e:
                if str(e) == 'Empty Response':
                    synapse_codes [ index ]= bittensor.proto.ReturnCode.EmptyResponse
                else:
                    synapse_codes [ index ]= bittensor.proto.ReturnCode.ResponseShapeException

                synapse_call_times [ index ] = clock.time() - start_time
                synapse_messages [index] = "Synapse response shape exception with error: {}".format( str( e ) )
                synapse_responses [ index ] = synapse.empty()

            except Exception as e:
                synapse_codes [ index ]= bittensor.proto.ReturnCode.ResponseSerializationException
                synapse_call_times [ index ] = clock.time() - start_time
                synapse_messages [index] = "Synapse response serialization exception with error: {}".format( str( e ) )
                synapse_responses [ index ] = synapse.empty()

            response_synapses.append(synapse.serialize_to_wire_proto(code = synapse_codes[index], message= synapse_messages[index] ))

            
        # Check if the call can stop here.
        if check_if_should_return():
            finalize_codes_stats_and_logs()
            return [], synapse_codes[0], request.synapses

        # =========================================================
        # ==== Set return times for successfull forward ===========
        # =========================================================
        for index, _ in enumerate( synapses ):
            if synapse_codes[index] == bittensor.proto.ReturnCode.Success:
                synapse_call_times[index] = clock.time() - start_time

        finalize_codes_stats_and_logs()
        return synapse_responses, bittensor.proto.ReturnCode.Success, response_synapses
 
    def _backward(self, request):
        r""" Performs validity checks on the grpc request before piping the request to the backend queue.
            Returns the outputs and synapses (with codes and messages from the backward call.)
            Args:
                request (:obj:`bittensor.proto`, `required`): 
                    Tensor request proto.
            Returns:
                response: (:obj:`bittensor.proto.Tensor, `required`): 
                    serialized tensor gradient responses. This is always an empty vector until gradients are allowed.
                code (:obj:`bittensor.proto.ReturnCode`, `required`):
                    Code from the call. This specifies if the overall function call was a success. 
                    This is separate from the synapse returns codes which relate to the individual synapse call. 
                synapses (:obj:`List[ 'bittensor.proto.Synapse' ]` of shape :obj:`(num_synapses)`, `required`):
                    Synapse wire protos with return codes from forward request.
        """

        # ===================================================================
        # ==== First deserialize synapse wire protos to instance objects ====        
        # ===================================================================
        synapses: List['bittensor.Synapse'] = []
        for synapse_wire_proto in request.synapses:
            synapses.append( bittensor.synapse.deserialize( synapse_wire_proto ) )

        # ===================================
        # ==== Init params from synapses ====        
        # ===================================
        # These items are filled through the call and the function returns 
        # when all codes are non-success or the function finishes completely.
        synapse_messages = [ "Success" for _ in synapses ]
        synapse_codes = [ bittensor.proto.ReturnCode.Success for _ in synapses ]
        deserialized_forward_tensors = [ None for _ in synapses ]
        deserialized_forward_gradients = [ None for _ in synapses ]
        synapse_is_response = [ False for _ in synapses ]
        synapse_call_times = [ 0 for _ in synapses ]
        start_time = clock.time()

        # ==================================================================
        # ==== Function which returns true if all codes are non success ====
        # ==================================================================
        def check_if_should_return() -> bool:
            for code in synapse_codes:
                if code == bittensor.proto.ReturnCode.Success:
                    return False
            return True

        # ==============================================================
        # ==== Function which prints all log statements per synapse ====
        # ==============================================================
        def finalize_codes_stats_and_logs():
            if self.prometheus:
                self.total_backward.inc()
                self.backward_latency.observe( clock.time() - start_time )
                self.backward_hotkeys.labels( request.hotkey ).inc()
                self.backward_bytes.labels( request.hotkey ).inc( sys.getsizeof( request.hotkey) )
            for index, synapse in enumerate( synapses ):
                if self.prometheus:
                    self.backward_synapses.labels( str(synapse) ).inc()
                    self.backward_codes.labels( str(synapse_codes[ index ]) ).inc()
                request.synapses [ index ].return_code = synapse_codes[ index ] # Set synapse wire proto codes.
                request.synapses [ index ].message = synapse_messages[ index ] # Set synapse wire proto message
                bittensor.logging.rpc_log ( 
                    axon = True, 
                    forward = False, 
                    is_response = synapse_is_response [index], 
                    code = synapse_codes[ index ], 
                    call_time = synapse_call_times[ index ], 
                    pubkey = request.hotkey, 
                    inputs = None if deserialized_forward_gradients[index] == None else deserialized_forward_gradients[index].shape  , 
                    outputs = None, # we never return from backward. 
                    message = synapse_messages[ index ],
                    synapse = synapse.synapse_type
                )

        # ======================================
        # ==== Check Empty request ====
        # ======================================
        if len(request.tensors) == 0:
            code = bittensor.proto.ReturnCode.EmptyRequest
            message = "Empty Request"
            call_time = clock.time() - start_time
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], code, request.synapses

        # ======================================
        # ==== Check Invalid request ====
        # ======================================
        if len(request.tensors) < 2:
            code = bittensor.proto.ReturnCode.InvalidRequest
            message = "Backward request contains {} tensors, expected atleast 2 tensor in the backward call".format(len(request.tensors))
            call_time = clock.time() - start_time
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], code, request.synapses

        # ======================================
        # ==== Check request length ====
        # ======================================
        if len( request.tensors ) != 2 * len( synapses ): # 2 per synapse (1 input + 1 grad).
            # Not enough responses per request.
            code = bittensor.proto.ReturnCode.RequestShapeException
            call_time = clock.time() - start_time
            message = "Request length doesn't match synape length."
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], code, request.synapses

        # ===================================
        # ==== Deserialize/Decode inputs ====
        # ===================================
        for index, synapse in enumerate( synapses ):
            try:
                deserialized_forward_tensors [index] = synapse.deserialize_forward_request_tensor ( request.tensors [index] )
                deserialized_forward_gradients [index] = synapse.deserialize_backward_request_gradient ( deserialized_forward_tensors [index],  request.tensors [ len( synapses ) + index ] )

            except ValueError as e:
                synapse_codes [index] = bittensor.proto.ReturnCode.RequestShapeException
                synapse_call_times [index] = clock.time() - start_time
                synapse_messages [index] = 'Input shape exception with error:{}'.format(str(e))

            except Exception as e:
                synapse_codes [index] = bittensor.proto.ReturnCode.RequestDeserializationException
                synapse_call_times [index] = clock.time() - start_time
                synapse_messages [index] = 'Input deserialization exception with error:{}'.format(str(e))
        # Check if the call can stop here.
        if check_if_should_return():
            finalize_codes_stats_and_logs()
            return [], synapse_codes[0], request.synapses


        # ===================================
        # ==== Make backward calls. =========
        # ===================================
        try:
            finalize_codes_stats_and_logs()
            synapse_is_response = [ True for _ in synapses ]
            if self.priority != None:
                # No wait on backward calls.
                priority = self.priority( request.hotkey, inputs_x = deserialized_forward_tensors, request_type = bittensor.proto.RequestType.BACKWARD )
                self.priority_threadpool.submit(
                    self.backward_callback, 
                    inputs_x = deserialized_forward_tensors, 
                    grads_dy = deserialized_forward_gradients,
                    synapses = synapses,
                    priority = priority
                )

            else:
                # Calling default
                backward_response_tensors, backward_codes, backward_messages = self.backward_callback ( deserialized_forward_tensors, deserialized_forward_gradients, synapses = synapses )
            
                # ========================================
                # ==== Fill codes from forward calls ====
                # ========================================
                for index, synapse in enumerate(synapses):
                    synapse_codes [ index ] = backward_codes [ index ]
                    synapse_messages [index] = backward_messages [ index ]

        # ========================================
        # ==== Catch backward request timeouts ====
        # ========================================
        except concurrent.futures.TimeoutError:
            code = bittensor.proto.ReturnCode.Timeout
            call_time = clock.time() - start_time
            message = "Request reached timeout"
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.Timeout, request.synapses

        # ==================================
        # ==== Catch unknown exceptions ====
        # ==================================
        except Exception as e:
            code = bittensor.proto.ReturnCode.UnknownException
            call_time = clock.time() - start_time
            message = str ( e )
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.UnknownException, request.synapses

        # Check if the call can stop here.
        if check_if_should_return():
            finalize_codes_stats_and_logs()
            return [], synapse_codes[0], request.synapses

        # ==============================
        # ==== Finalize call times =====
        # ==============================
        for index, _ in enumerate( synapses ):
            if synapse_codes[index] == bittensor.proto.ReturnCode.Success:
                synapse_call_times[index] = clock.time() - start_time

        finalize_codes_stats_and_logs()
        return [], bittensor.proto.ReturnCode.Success, request.synapses

    def default_forward_callback(self, inputs_x:torch.FloatTensor, synapses=[], hotkey = None):
        """
            The default forward callback when no callback is attached: Is used to call specific synapse functions

            Args:
                inputs_x (:obj:`torch.FloatTensor`, `required`): 
                    The inputs that will be passed to the synapse functions
                
                synapses (:obj: list of bittensor.proto.SynapseArgs, 'Optional')
                    The proto message that contains additional args for individual synapse functions

                hotkey (:obj: str of the hotkey, 'Optional')
                    The hotkey of the validator who sent the request

            Returns:
                response_tensors: (:obj: list of bittensor.proto.Tensor, `required`): 
                    serialized tensor response from the nucleus call or None.
                response_codes: (:obj: list of bittensor.proto.ReturnCode, `required`)
                    return code associated with forward call i.e. Success of Timeout.
                response_messages: (:obj: list of strings, `required`)
                    return message associated with synapse call
        """
        # --- initialize response variables --- 
        response_tensors = []
        response_codes = []
        response_messages = []
        model_output = None
        
        # --- calling attached synapses ---
        for index, synapse in enumerate(synapses):
            try:
                synapse_check =  self.synapse_checks(synapse, hotkey)

                if synapse.synapse_type in self.synapse_callbacks and self.synapse_callbacks[synapse.synapse_type] != None and synapse_check:
                    message, model_output, response_tensor = self.synapse_callbacks[synapse.synapse_type](inputs_x[index], synapse, model_output)
                    response_tensors.append(response_tensor)
                    response_codes.append(bittensor.proto.ReturnCode.Success)
                    response_messages.append('Success' if message is None else message)
                
                elif not synapse_check:
                    response_tensors.append(None)
                    response_codes.append(bittensor.proto.ReturnCode.UnknownException)
                    response_messages.append('Synapse Check Failed')

                else:
                    response_tensors.append(None)
                    response_codes.append(bittensor.proto.ReturnCode.NotImplemented)
                    response_messages.append('Not Implemented')

            except Exception as e: 
                # --- Exception Hit in Synapse ---
                response_tensors.append(None)
                response_codes.append(bittensor.proto.ReturnCode.UnknownException)
                response_messages.append(str(e))
        
        return response_tensors, response_codes, response_messages

    def default_backward_callback(self, inputs_x:torch.FloatTensor, grads_dy:torch.FloatTensor, synapses=[] ):
        """
            The default backward callback when no callback is attached: Is used to call specific synapse functions

            Args:
                inputs_x (:obj:`torch.FloatTensor`, `required`): 
                    The inputs that will be passed to the synapse functions
                grads_dy (:obj:`torch.FloatTensor`, `required`):
                    The gradients that will be passed to the synapse functions
                synapses (:obj: list of bittensor.proto.SynapseArgs, 'Optional')
                    The proto message that contains additional args for individual synapse functions

            Returns:
                response_tensors: (:obj: list of bittensor.proto.Tensor, `required`): 
                    serialized tensor response from the nucleus call or None.
                response_codes: (:obj: list of bittensor.proto.ReturnCode, `required`)
                    return code associated with forward call i.e. Success of Timeout.
                response_messages: (:obj: list of strings, `required`)
                    return message associated with synapse call
        """
        # --- initialize response variables --- 
        response_tensors = []
        response_codes = []
        response_messages = []
        
        # --- calling attached synapses ---
        with torch.enable_grad() and torch.autograd.set_detect_anomaly(True):
            for index, synapse in enumerate(synapses):
                try:
                    if synapse.synapse_type in self.synapse_callbacks and self.synapse_callbacks[synapse.synapse_type] != None:
                        message, model_output, response_tensor = self.synapse_callbacks[synapse.synapse_type](inputs_x[index], synapse)
                        torch.autograd.backward (
                            tensors = [ response_tensor ],
                            grad_tensors = [ grads_dy[index] ],
                            retain_graph=True
                        )                        
                        response_tensors.append(None)
                        response_codes.append(bittensor.proto.ReturnCode.Success)
                        response_messages.append('Success')
                    else:
                        response_tensors.append(None)
                        response_codes.append(bittensor.proto.ReturnCode.NotImplemented)
                        response_messages.append('Not Implemented')
                except Exception as e:
                    # --- Exception Hit in Synapse ---
                    response_tensors.append(None)
                    response_codes.append(bittensor.proto.ReturnCode.UnknownException)
                    response_messages.append(str(e))

        if self.optimizer_step != None:
            self.optimizer_step()
        
        return response_tensors, response_codes, response_messages

    def attach_forward_callback(self, forward_callback: Callable[ [str, torch.Tensor, int], torch.Tensor ]):
        """ Assigns the forward_callback.

            Returns:
                forward_callback (:callabl:`Callable[ [str, torch.Tensor, int], torch.Tensor `, `required`): 
                    Forward function called on recieving a forward request.
        """
        bittensor.axon.check_forward_callback(forward_callback)
        self.forward_callback = forward_callback

    def attach_synapse_callback(self, synapse_callback: Callable[[str, torch.Tensor, int],torch.Tensor], synapse_type ):
        """ Assigns the callback to a specific synapse.

            Args:
                synapse_callback (:callabl:`Callable[ [str, torch.Tensor, int], torch.Tensor `, `required`): 
                    function called for a specific synapse.
        """
        self.synapse_callbacks[synapse_type] = synapse_callback

    def attach_backward_callback(self, backward_callback: Callable[ [str, torch.Tensor, torch.Tensor, int], torch.Tensor ] ):
        """ Assigns the backward_callback call to this neuron.

            Returns:
                backward_callback (:callabl:`Callable[ [torch.Tensor, torch.Tensor], torch.Tensor `, `required`): 
                     Backward callback called on recieving a backward request.
        """
        bittensor.axon.check_backward_callback(backward_callback)
        self.backward_callback = backward_callback

    def __del__(self):
        r""" Called when this axon is deleted, ensures background threads shut down properly.
        """
        self.stop()

    def serve( 
            self, 
            use_upnpc: bool = False, 
            subtensor: 'bittensor.Subtensor' = None,
            network: str = None,
            chain_endpoint: str = None,
            prompt: bool = False
        ) -> 'Axon':
        r""" Subscribes this Axon servicing endpoint to the passed network using it's wallet.
            Args:
                use_upnpc (:type:bool, `optional`): 
                    If true, serves the axon attempts port forward through your router before 
                    subscribing.
                subtensor (:obj:`bittensor.Subtensor`, `optional`): 
                    Chain connection through which to serve.
                network (default='local', type=str)
                    If subtensor is not set, uses this network flag to create the subtensor connection.
                chain_endpoint (default=None, type=str)
                    Overrides the network argument if not set.
                prompt (bool):
                    If true, the call waits for confirmation from the user before proceeding.

        """   
        if subtensor == None: subtensor = bittensor.subtensor( network = network, chain_endpoint = chain_endpoint) 
        serv_success = subtensor.serve_axon( axon = self, use_upnpc = use_upnpc, prompt = prompt )
        if not serv_success:
            raise RuntimeError('Failed to serve neuron.')
        return self

    def start(self) -> 'Axon':
        r""" Starts the standalone axon GRPC server thread.
        """
        if self.server != None:
            self.server.stop( grace = 1 )  
            logger.success("Axon Stopped:".ljust(20) + "<blue>{}</blue>", self.ip + ':' + str(self.port))

        self.server.start()
        logger.success("Axon Started:".ljust(20) + "<blue>{}</blue>", self.ip + ':' + str(self.port))
        self.started = True
        if self.prometheus:
            self.is_started.state('started')
        return self

    def stop(self) -> 'Axon':
        r""" Stop the axon grpc server.
        """
        if self.server != None:
            self.server.stop( grace = 1 )
            logger.success("Axon Stopped:".ljust(20) + "<blue>{}</blue>", self.ip + ':' + str(self.port))
        self.started = False
        if self.prometheus:
            self.is_started.state('stopped')
        return self