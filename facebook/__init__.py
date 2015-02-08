#!/usr/bin/env python
#
# Copyright 2010 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
Python client library for the Facebook Platform.

This client library is designed to support the Graph API and the
official Facebook JavaScript SDK, which is the canonical way to
implement Facebook authentication. Read more about the Graph API at
http://developers.facebook.com/docs/api. You can download the Facebook
JavaScript SDK at http://github.com/facebook/connect-js/.

If your application is using Google AppEngine's webapp framework, your
usage of this module might look like this:

import facebook
auth = facebook.Auth(key, secret)
user = auth.get_user_from_cookie(self.request.cookies)
if user:
    graph = facebook.GraphAPI(user["access_token"])
    profile = graph.get_object("me")
    friends = graph.get_connections("me", "friends")

"""

import hashlib
import hmac
import base64
import requests
import json
import re

try:
    from urllib.parse import parse_qs, urlencode
except ImportError:
    from urlparse import parse_qs
    from urllib import urlencode

from . import version


__version__ = version.__version__


class GraphAPI(object):
    """A client for the Facebook Graph API.

    See http://developers.facebook.com/docs/api for complete
    documentation for the API.

    The Graph API is made up of the objects in Facebook (e.g., people,
    pages, events, photos) and the connections between them (e.g.,
    friends, photo tags, and event RSVPs). This client provides access
    to those primitive types in a generic way. For example, given an
    OAuth access token, this will fetch the profile of the active user
    and the list of the user's friends:

       graph = facebook.GraphAPI(access_token)
       user = graph.get_object("me")
       friends = graph.get_connections(user["id"], "friends")

    You can see a list of all of the objects and connections supported
    by the API at http://developers.facebook.com/docs/reference/api/.

    You can obtain an access token via OAuth or by using the Facebook
    JavaScript SDK. See
    http://developers.facebook.com/docs/authentication/ for details.

    If you are using the JavaScript SDK, you can use the
    get_user_from_cookie() method below to get the OAuth access token
    for the active user from the cookie saved by the SDK.

    """

    def __init__(self, access_token=None, timeout=None, version="2.2"):
        version = str(version)
        valid_api_versions = ["1.0", "2.0", "2.1", "2.2"]

        self.access_token = access_token
        self.timeout = timeout

        if version not in valid_api_versions:
            raise GraphAPIError("Valid API versions are {}".format(
                ", ".join(valid_api_versions)))

        self.version = "v" + version

    def get_object(self, id, **args):
        """Fetchs the given object from the graph."""
        return self.request(id, args)

    def get_objects(self, ids, **args):
        """Fetchs all of the given object from the graph.

        We return a map from ID to object. If any of the IDs are
        invalid, we raise an exception.
        """
        args["ids"] = ",".join(ids)
        return self.request(args)

    def get_connections(self, id, connection_name, **args):
        """Fetchs the connections for given object."""
        return self.request("{0}/{1}".format(id, connection_name), args)

    def put_object(self, parent_object, connection_name, **data):
        """Writes the given object to the graph, connected to the given parent.

        For example,

            graph.put_object("me", "feed", message="Hello, world")

        writes "Hello, world" to the active user's wall. Likewise, this
        will comment on a the first post of the active user's feed:

            feed = graph.get_connections("me", "feed")
            post = feed["data"][0]
            graph.put_object(post["id"], "comments", message="First!")

        See http://developers.facebook.com/docs/api#publishing for all
        of the supported writeable objects.

        Certain write operations require extended permissions. For
        example, publishing to a user's feed requires the
        "publish_actions" permission. See
        http://developers.facebook.com/docs/publishing/ for details
        about publishing permissions.

        """
        assert self.access_token, "Write operations require an access token"
        return self.request("{0}/{1}".format(parent_object, connection_name),
            post_args=data, method="POST")

    def put_wall_post(self, message, attachment={}, profile_id="me"):
        """Writes a wall post to the given profile's wall.

        We default to writing to the authenticated user's wall if no
        profile_id is specified.

        attachment adds a structured attachment to the status message
        being posted to the Wall. It should be a dictionary of the form:

            {"name": "Link name"
             "link": "http://www.example.com/",
             "caption": "{*actor*} posted a new review",
             "description": "This is a longer description of the attachment",
             "picture": "http://www.example.com/thumbnail.jpg"}

        """
        return self.put_object(profile_id, "feed", message=message,
                               **attachment)

    def put_comment(self, object_id, message):
        """Writes the given comment on the given post."""
        return self.put_object(object_id, "comments", message=message)

    def put_like(self, object_id):
        """Likes the given post."""
        return self.put_object(object_id, "likes")

    def delete_object(self, id):
        """Deletes the object with the given ID from the graph."""
        self.request(id, method="DELETE")

    def delete_request(self, user_id, request_id):
        """Deletes the Request with the given ID for the given user."""
        self.request("%s_%s" % (request_id, user_id), method="DELETE")

    def put_photo(self, image, album_path="me/photos", **kwargs):
        """
        Upload an image using multipart/form-data.

        image - A file object representing the image to be uploaded.
        album_path - A path representing where the image should be uploaded.

        """
        return self.request(
            album_path,
            post_args=kwargs,
            files={"source": image},
            method="POST")

    def get_version(self):
        """Fetches the current version number of the Graph API being used."""
        args = {"access_token": self.access_token}
        try:
            response = requests.get("https://graph.facebook.com/" +
                self.version, params=args, timeout=self.timeout)
        except requests.HTTPError as e:
            # We expect an Unauthenticated error, as we don't send a token
            if e.status != 400:
                response = json.loads(e.read())
                raise GraphAPIError(response)

        try:
            headers = response.headers
            version = headers["facebook-api-version"].replace("v", "")
            return version
        except Exception:
            raise GraphAPIError("API version number not available")

    def request(self, path, args=None, post_args=None, files=None, method=None):
        """
        Fetches the given path in the Graph API.

        We translate args to a valid query string. If post_args is
        given, we send a POST request to the given path with the given
        arguments.

        """
        args = args or {}

        if self.access_token:
            if post_args is not None:
                post_args["access_token"] = self.access_token
            else:
                args["access_token"] = self.access_token

        url = "https://graph.facebook.com/{0}/{1}".format(self.version, path)
        try:
            response = requests.request(method or "GET",
                                        url,
                                        timeout=self.timeout,
                                        params=args,
                                        data=post_args,
                                        files=files)
        except requests.HTTPError as e:
            response = json.loads(e.read())
            raise GraphAPIError(response)

        headers = response.headers
        if 'json' in headers['content-type']:
            result = response.json()
        elif 'image/' in headers['content-type']:
            mimetype = headers['content-type']
            result = {"data": response.content,
                      "mime-type": mimetype,
                      "url": response.url}
        elif "access_token" in parse_qs(response.text):
            query_str = parse_qs(response.text)
            if "access_token" in query_str:
                result = {"access_token": query_str["access_token"][0]}
                if "expires" in query_str:
                    result["expires"] = query_str["expires"][0]
            else:
                raise GraphAPIError(response.json())
        else:
            raise GraphAPIError('Maintype was not text, image, or querystring')

        if result and isinstance(result, dict) and result.get("error"):
            raise GraphAPIError(result)
        return result

    def fql(self, query):
        """
        FQL query.

        Example query: "SELECT affiliations FROM user WHERE uid = me()"
        """
        if self.version not in ("v1.0", "v2.0"):
            raise GraphAPIError("Versions later than 2.0 don't support FQL")
        return self.request("fql", {"q": query})


class GraphAPIError(Exception):
    def __init__(self, result):
        self.result = result
        try:
            self.type = result["error_code"]
        except:
            self.type = ""

        # OAuth 2.0 Draft 10
        try:
            self.message = result["error_description"]
        except:
            # OAuth 2.0 Draft 00
            try:
                self.message = result["error"]["message"]
            except:
                # REST server style
                try:
                    self.message = result["error_msg"]
                except:
                    self.message = result

        Exception.__init__(self, self.message)


class AuthError(GraphAPIError):
    pass


class Auth(object):
    """
    Class for dealing with authentication.
    It is setup with the app_id and app_secret.
    """

    def __init__(self, app_id, app_secret, redirect_uri):
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri

    def get_user_from_cookie(self, cookies, validate=False):
        """
        Parses the cookie set by the official Facebook JavaScript SDK.

        cookies should be a dictionary-like object mapping cookie names to
        cookie values.

        If the user is logged in via Facebook, we return a dictionary with the
        keys "uid" and "access_token". The former is the user's Facebook ID, and
        the latter can be used to make authenticated requests to the Graph API.
        If the user is not logged in, we return None.

        Download the official Facebook JavaScript SDK at
        http://github.com/facebook/connect-js/. Read more about Facebook
        authentication at http://developers.facebook.com/docs/authentication/.

        """
        cookie = cookies.get("fbsr_" + self.app_id, "")
        if not cookie:
            return None

        try:
            user_data = self.parse_signed_request(cookie)
        except ValueError as e:
            raise AuthError('Error parsing fbsr-cookie', e)

        if not user_data:
            return None

        if validate:
            try:
                result = self.get_access_token_from_code(user_data["code"])
            except GraphAPIError:
                return None

        user_data.update(result)
        return result

    def parse_signed_request(self, signed_request):
        """
        Return dictionary with signed request data.

        We return a dictionary containing the information in the signed_request.
        This includes a user_id if the user has authorised your application, as
        well as any information requested.

        If the signed_request is malformed or corrupted, False is returned.
        """
        try:
            encoded_sig, payload = map(str, signed_request.split('.', 1))

            sig = base64.urlsafe_b64decode(encoded_sig + "=" *
                                           ((4 - len(encoded_sig) % 4) % 4))
            data = base64.urlsafe_b64decode(payload + "=" *
                                            ((4 - len(payload) % 4) % 4))
        except IndexError:
            raise ValueError('signed_request malformed')
        except TypeError:
            raise ValueError('signed_request had corrupted payload')

        data = json.loads(data)
        if data.get('algorithm', '').upper() != 'HMAC-SHA256':
            raise ValueError('signed_request used unknown algorithm')

        # HMAC can only handle ascii (byte) strings
        # http://bugs.python.org/issue5285
        self.app_secret = self.app_secret.encode('ascii')
        payload = payload.encode('ascii')

        expected_sig = hmac.new(self.app_secret,
                                msg=payload,
                                digestmod=hashlib.sha256).digest()
        if sig != expected_sig:
            raise ValueError('signed_request had signature mismatch')

        return data

    def auth_url(self, canvas_url, perms=None, **kwargs):
        url = "https://www.facebook.com/dialog/oauth?"
        kvps = {'client_id': self.app_id, 'redirect_uri': canvas_url}
        if perms:
            kvps['scope'] = ",".join(perms)
        kvps.update(kwargs)
        return url + urlencode(kvps)

    def get_app_access_token(self):
        """Get the application's access token as a string."""
        args = {'grant_type': 'client_credentials',
                'client_id': self.app_id,
                'client_secret': self.app_secret}

        return GraphAPI().request("oauth/access_token", args=args)["access_token"]

    def get_access_token_from_code(self, code):
        """
        Get an access token from the "code" returned from an OAuth dialog.

        Returns a dict containing the user-specific access token and its
        expiration date (if applicable).
        """
        args = {
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.app_id,
            "client_secret": self.app_secret}

        return GraphAPI().request("oauth/access_token", args)

    def extend_access_token(self, app_id, app_secret):
        """
        Extends the expiration time of a valid OAuth access token. See
        <https://developers.facebook.com/roadmap/offline-access-removal/
        #extend_token>
        """
        args = {
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "grant_type": "fb_exchange_token",
            "fb_exchange_token": self.access_token,
        }

        return GraphAPI().request("oauth/access_token", args=args)
