# Generated by the gRPC Python protocol compiler plugin. DO NOT EDIT!
import grpc

import bittensor_pb2 as bittensor__pb2


class BittensorStub(object):
    """NOTE(const): Not used.
    TODO(const): Switch to Bittensor protocol.

    """

    def __init__(self, channel):
        """Constructor.

        Args:
            channel: A grpc.Channel.
        """
        self.Spike = channel.unary_unary(
                '/Bittensor/Spike',
                request_serializer=bittensor__pb2.SpikeRequest.SerializeToString,
                response_deserializer=bittensor__pb2.SpikeResponse.FromString,
                )
        self.Grade = channel.unary_unary(
                '/Bittensor/Grade',
                request_serializer=bittensor__pb2.GradeRequest.SerializeToString,
                response_deserializer=bittensor__pb2.GradeResponse.FromString,
                )


class BittensorServicer(object):
    """NOTE(const): Not used.
    TODO(const): Switch to Bittensor protocol.

    """

    def Spike(self, request, context):
        """Query remote component with text-features, responses are var-length vector
        representations of the text.
        """
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def Grade(self, request, context):
        """Query a remote component with gradients. Responses are boolean affirmatives.
        """
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')


def add_BittensorServicer_to_server(servicer, server):
    rpc_method_handlers = {
            'Spike': grpc.unary_unary_rpc_method_handler(
                    servicer.Spike,
                    request_deserializer=bittensor__pb2.SpikeRequest.FromString,
                    response_serializer=bittensor__pb2.SpikeResponse.SerializeToString,
            ),
            'Grade': grpc.unary_unary_rpc_method_handler(
                    servicer.Grade,
                    request_deserializer=bittensor__pb2.GradeRequest.FromString,
                    response_serializer=bittensor__pb2.GradeResponse.SerializeToString,
            ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
            'Bittensor', rpc_method_handlers)
    server.add_generic_rpc_handlers((generic_handler,))


 # This class is part of an EXPERIMENTAL API.
class Bittensor(object):
    """NOTE(const): Not used.
    TODO(const): Switch to Bittensor protocol.

    """

    @staticmethod
    def Spike(request,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.unary_unary(request, target, '/Bittensor/Spike',
            bittensor__pb2.SpikeRequest.SerializeToString,
            bittensor__pb2.SpikeResponse.FromString,
            options, channel_credentials,
            call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def Grade(request,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.unary_unary(request, target, '/Bittensor/Grade',
            bittensor__pb2.GradeRequest.SerializeToString,
            bittensor__pb2.GradeResponse.FromString,
            options, channel_credentials,
            call_credentials, compression, wait_for_ready, timeout, metadata)