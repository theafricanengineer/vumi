# -*- test-case-name: vumi.persist.tests.test_riak_manager -*-

"""A manager implementation on top of the riak Python package."""

import json

from riak import (
    RiakClient, RiakObject, RiakMapReduce, RiakHttpTransport, RiakPbcTransport)

from vumi.persist.model import Manager
from vumi.utils import flatten_generator


class RiakManager(Manager):
    """A persistence manager for the riak Python package."""

    call_decorator = staticmethod(flatten_generator)

    @classmethod
    def from_config(cls, config):
        config = config.copy()
        bucket_prefix = config.pop('bucket_prefix')
        load_bunch_size = config.pop('load_bunch_size',
                                     cls.DEFAULT_LOAD_BUNCH_SIZE)
        mapreduce_timeout = config.pop('mapreduce_timeout',
                                       cls.DEFAULT_MAPREDUCE_TIMEOUT)
        transport_type = config.pop('transport_type', 'http')
        http_transport_class = RiakHttpTransport
        if cls.USE_STREAMING_MAPREDUCE:
            http_transport_class = StreamingMapReduceHttpTransport
        transport_class = {
            'http': http_transport_class,
            'protocol_buffer': RiakPbcTransport,
        }.get(transport_type, http_transport_class)

        host = config.get('host', '127.0.0.1')
        port = config.get('port', 8098)
        prefix = config.get('prefix', 'riak')
        mapred_prefix = config.get('mapred_prefix', 'mapred')
        client_id = config.get('client_id')
        # NOTE: the current riak.RiakClient expects this parameter but
        #       internally doesn't do anything with it.
        solr_transport_class = config.get('solr_transport_class', None)
        transport_options = config.get('transport_options', None)

        client = RiakClient(host=host, port=port, prefix=prefix,
            mapred_prefix=mapred_prefix, transport_class=transport_class,
            client_id=client_id, solr_transport_class=solr_transport_class,
            transport_options=transport_options)
        # Some versions of the riak client library use simplejson by
        # preference, which breaks some of our unicode assumptions. This makes
        # sure we're using stdlib json which doesn't sometimes return
        # bytestrings instead of unicode.
        client.set_encoder('application/json', json.dumps)
        client.set_encoder('text/json', json.dumps)
        client.set_decoder('application/json', json.loads)
        client.set_decoder('text/json', json.loads)
        return cls(client, bucket_prefix, load_bunch_size=load_bunch_size,
                   mapreduce_timeout=mapreduce_timeout)

    def riak_object(self, modelcls, key, result=None):
        bucket = self.bucket_for_modelcls(modelcls)
        riak_object = RiakObject(self.client, bucket, key)
        if result:
            metadata = result['metadata']
            indexes = metadata['index']
            if hasattr(indexes, 'items'):
                # TODO: I think this is a Riak bug. In some cases
                #       (maybe when there are no indexes?) the index
                #       comes back as a list, in others (maybe when
                #       there are indexes?) it comes back as a dict.
                indexes = indexes.items()
            data = result['data']
            riak_object.set_content_type(metadata['content-type'])
            riak_object.set_indexes(indexes)
            riak_object.set_encoded_data(data)
        else:
            riak_object.set_data({'$VERSION': modelcls.VERSION})
            riak_object.set_content_type("application/json")
        return riak_object

    def store(self, modelobj):
        modelobj._riak_object.store()
        return modelobj

    def delete(self, modelobj):
        modelobj._riak_object.delete()

    def load(self, modelcls, key, result=None):
        riak_object = self.riak_object(modelcls, key, result)
        if not result:
            riak_object.reload()

        # Run migrators until we have the correct version of the data.
        while riak_object.get_data() is not None:
            data_version = riak_object.get_data().get('$VERSION', None)
            if data_version == modelcls.VERSION:
                return modelcls(self, key, _riak_object=riak_object)
            migrator = modelcls.MIGRATOR(modelcls, self, data_version)
            riak_object = migrator(riak_object).get_riak_object()
        return None

    def _load_multiple(self, modelcls, keys):
        objs = (self.load(modelcls, key) for key in keys)
        return [obj for obj in objs if obj is not None]

    def riak_map_reduce(self):
        return RiakMapReduce(self.client)

    def run_map_reduce(self, mapreduce, mapper_func=None, reducer_func=None):
        results = mapreduce.run(timeout=self.mapreduce_timeout)
        if mapper_func is not None:
            results = [mapper_func(self, row) for row in results]
        if reducer_func is not None:
            results = reducer_func(self, results)
        return results

    def riak_enable_search(self, modelcls):
        bucket_name = self.bucket_name(modelcls)
        bucket = self.client.bucket(bucket_name)
        return bucket.enable_search()

    def purge_all(self):
        buckets = self.client.get_buckets()
        for bucket_name in buckets:
            if bucket_name.startswith(self.bucket_prefix):
                bucket = self.client.bucket(bucket_name)
                for key in bucket.get_keys():
                    obj = bucket.get(key)
                    obj.delete()


class StreamingMapReduceHttpTransport(RiakHttpTransport):
    def mapred(self, inputs, query, timeout=None):
        """
        Run a MapReduce query.

        Pilfered from riak-python 1.5.2 and modified to handle multipart data.
        """
        if not self.phaseless_mapred() and (query is None or len(query) is 0):
            raise Exception(
                'Phase-less MapReduce is not supported by Riak node')

        # Construct the job, optionally set the timeout...
        job = {'inputs': inputs, 'query': query}
        if timeout is not None:
            job['timeout'] = timeout

        content = json.dumps(job)

        # Do the request...
        url = "/%s?chunked=true" % (self._mapred_prefix,)
        headers = {'Content-Type': 'application/json'}
        response = self.http_request('POST', url, headers, content)

        # Make sure the expected status code came back...
        status = response[0]['http_code']
        if status != 200:
            self.raise_mapred_error(response[0], response[1])
        return self.decode_chunked_response(response[0], response[1])

    def raise_mapred_error(self, headers, body):
        raise Exception(
            'Error running MapReduce operation. Headers: %s Body: %s' % (
                repr(headers), repr(body)))

    def decode_chunked_response(self, headers, body):
        fake_email = "Content-Type: %s\n\n%s" % (headers['content-type'], body)
        from email import message_from_string
        msg = message_from_string(fake_email)
        if msg.is_multipart():
            return self.decode_chunks(msg.get_payload())

        payload = msg.get_payload()
        if not payload.strip():
            # No content means no results.
            payload = "[]"
        result = json.loads(payload)
        if isinstance(result, dict) and 'error' in result:
            self.raise_mapred_error(headers, body)

        return result

    def decode_chunks(self, chunks):
        phase_results = {}
        for chunk in chunks:
            part = json.loads(chunk.get_payload())
            phase_results.setdefault(part['phase'], []).extend(part['data'])
        # NOTE: We discard all but the last phase received.
        return phase_results[max(phase_results.keys())]
