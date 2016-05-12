# Copyright (c) 2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import base64
import json
import urllib

from swift import gettext_ as _
from swift.common.exceptions import EncryptionException
from swift.common.swob import HTTPInternalServerError
from swift.common.wsgi import WSGIContext
from swift.common.request_helpers import strip_sys_meta_prefix, \
    strip_object_transient_sysmeta_prefix
from swift.common.swob import HTTPBadRequest

CRYPTO_KEY_CALLBACK = 'swift.callback.fetch_crypto_keys'


class CryptoWSGIContext(WSGIContext):
    """
    Base class for contexts used by crypto middlewares.
    """
    def __init__(self, crypto_app, server_type, logger):
        super(CryptoWSGIContext, self).__init__(crypto_app.app)
        self.crypto = crypto_app.crypto
        self.logger = logger
        self.server_type = server_type

    def get_keys(self, env, required=None):
        # Get the key(s) from the keymaster
        required = required if required is not None else [self.server_type]
        try:
            fetch_crypto_keys = env[CRYPTO_KEY_CALLBACK]
        except KeyError:
            self.logger.exception(_(
                'ERROR get_keys() %s not in env') % CRYPTO_KEY_CALLBACK)
            raise HTTPInternalServerError(
                "Unable to retrieve encryption keys.")

        try:
            keys = fetch_crypto_keys()
        except Exception as err:  # noqa
            self.logger.exception(_(
                'ERROR get_keys(): from %(callback)s: %(err)s'),
                {'callback': CRYPTO_KEY_CALLBACK, 'err': str(err)})
            raise HTTPInternalServerError(
                "Unable to retrieve encryption keys.")

        for name in required:
            try:
                key = keys[name]
                self.crypto.check_key(key)
                continue
            except KeyError:
                self.logger.exception(_("Missing key for %r") % name)
            except TypeError:
                self.logger.exception(_("Did not get a keys dict"))
            except ValueError as e:
                # don't include the key in any messages!
                self.logger.exception(_("Bad key for %r: %s") % (name, str(e)))
            raise HTTPInternalServerError(
                "Unable to retrieve encryption keys.")

        return keys


def dump_crypto_meta(crypto_meta):
    """
    Set the crypto-meta associated to the obj body.

    The IV value is random bytes and as a result needs to be encoded before
    sending over the wire. Do this by wrapping the crypto meta in a json
    object and encode the iv value. Base64 encoding returns a bytes object
    in py3, to future proof the code, decode this data to produce a string,
    which is what the json.dumps function expects.

    :param crypto_meta: a dict containing crypto meta items
    :returns: a string serialization of a crypto meta dict
    """
    return urllib.quote_plus(json.dumps({
        name: (base64.b64encode(value).decode() if name == 'iv' else value)
        for name, value in crypto_meta.items()}))


def load_crypto_meta(value):
    """
    Build the crypto_meta from the json object.

    Note that json.loads always produces unicode strings, to ensure the
    resultant crypto_meta matches the original object cast all key and
    value data (other then the iv) to a str. This will work in py3 as well
    where all strings are unicode implying the cast is effectively a no-op.

    :param value: a string serialization of a crypto meta dict
    :returns: a dict containing crypto meta items
    :raises EncryptionException: if an error occurs while parsing the
                                 crypto meta
    """
    try:
        value = urllib.unquote_plus(value)
        crypto_meta = {str(name): (base64.b64decode(value)
                                   if name == 'iv' else str(value))
                       for name, value in json.loads(value).items()}
        return crypto_meta
    except (KeyError, ValueError, TypeError) as err:
        msg = 'Bad crypto meta %s: %s' % (value, err)
        raise EncryptionException(msg)


def append_crypto_meta(value, crypto_meta):
    """
    Serialize and append crypto metadata to an encrypted value.

    :param value: value to which serialized crypto meta will be appended.
    :param crypto_meta: a dict of crypto meta
    :return: a string of the form <value>; meta=<serialized crypto meta>
    """
    return '%s; meta=%s' % (value, dump_crypto_meta(crypto_meta))


def extract_crypto_meta(value):
    """
    Extract and deserialize any crypto meta from the end of a value.

    :param value: string that may have crypto meta at end
    :return: a tuple of the form:
            (<value without crypto meta>, <deserialized crypto meta> or None)
    """
    crypto_meta = None
    parts = value.rsplit(';', 1)
    if len(parts) == 2:
        value, param = parts
        if param.strip().startswith('meta='):
            param = param.strip()[5:]
            crypto_meta = load_crypto_meta(param)
    return value, crypto_meta


def is_crypto_meta(header, server_type):
    return (strip_sys_meta_prefix(
        server_type, header.lower()).startswith('crypto-meta') or
        strip_object_transient_sysmeta_prefix(
        header.lower()).startswith('crypto-meta'))


def parse_header_keys(req):
    """
    Utility function to parse headers for BYOK

    :param req: request object
    :return: a dictionary with any parsed keys, empty if no keys are found in
             headers
    :raises: HTTPBadRequest if one of the keys in BYOK headers is missing
             or has wrong length
    """
    keys = {}
    if ('X-Crypto-Object-Key' in req.headers or
            'X-Crypto-Container-Key' in req.headers):

        def _validate_key(header_name):
            try:
                key = base64.b64decode(req.headers[header_name])
            except KeyError:
                raise HTTPBadRequest("%s is missing" % header_name)
            except TypeError:
                raise HTTPBadRequest("%s is an invalid format" % header_name)
            if len(key) != 32:
                raise HTTPBadRequest("%s length should be 32" % header_name)
            return key

        keys['object'] = _validate_key('X-Crypto-Object-Key')
        keys['container'] = _validate_key('X-Crypto-Container-Key')

    return keys
