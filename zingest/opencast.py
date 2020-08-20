import json
import requests
import wget
import os
from requests.auth import HTTPDigestAuth
import sys
import configparser

from zingest.rabbit import Rabbit
from zingest.zoom import Zoom

import logging
import zingest.logger

class Opencast:

    def __init__(self, config, rabbit):
        self.logger = logging.getLogger("opencast")
        self.logger.setLevel(logging.DEBUG)

        self.url = config["Opencast"]["Url"]
        self.logger.debug(f"Opencast url is {self.url}")
        self.user = config["Opencast"]["User"]
        self.logger.debug(f"Opencast user is {self.user}")
        self.password = config["Opencast"]["Password"]
        self.logger.debug(f"Opencast password is {self.password}")
        self.logger.info("Setup complete, consuming rabbits")
        if rabbit and type(rabbit) == zingest.rabbit.Rabbit:
            rabbit.start_consuming_rabbitmsg(self.rabbit_callback)
        else:
            raise TypeError("Rabbit is missing or the wrong type!")


    def rabbit_callback(self, method, properties, body):
        #TODO: Record ${body} somewhere (DB?) so that if we get abuptly killed we don't lose state!
        data, id = self.parse_queue(body)
        self.oc_upload(data["creator"], data["topic"], id)
        os.remove(id+'.mp4')


    def parse_queue(self, body):
        #NB: Previously decoded here, unsure if needed
        data = json.loads(body)#.decode("utf-8"))
        if "recording_files" not in data:
            self.logger.error("No recording found")
        files = data["recording_files"]
        
        dl_url = ''
        id = ''
        for key in files[0].keys():
            if key == "download_url":
                self.logger.debug(f"Download url found: {dl_url}")
                dl_url = files[0][key]
            elif key == "recording_id":
                id = files[0][key]
                self.logger.debug(f"Recording id found: {id}")

        self.logger.debug(f"Downloading from {dl_url}/?access_token={data['token']} to {id}.mp4")
        self._do_download(dl_url+'/?access_token='+data["token"], id+'.mp4')
        self.logger.debug(f"Id is ${id}")
        return data, id


    def _do_download(self, url, output):
        try:
            #TODO: Verify that this download succeeds (ie, with a checksum) rather than checking file existence
            wget.download(url, output)
        except Exception as e:
            self.logger.error("Could not download file {}".format(e))
            raise e
    


    def oc_upload(self, creator, title, rec_id):

        response = self._do_get(self.url + '/admin-ng/series/series.json')

        series_list = json.loads(response.content.decode("utf-8"))
        #TODO: Abort?  Or upload with a default user?
        try:
            self.username = creator[:creator.index("@")]
        except ValueError:
            self.logger.warning("Invalid username: '@' is missing, upload is aborted")
            return
        series_title = "Zoom Recordings " + username
        series_found = False
        for series in series_list["results"]:
            if series["title"] == series_title:
                series_found = True
                id = series["id"]

        #TODO: This needs to be optional, and/or support a default series
        if not series_found:
            #TODO: What if this fails?
            id = self.create_series(creator, series_title)

        with open(rec_id+'.mp4', 'rb') as fobj:
            #TODO: The flavour here needs to be configurable.  Maybe.
            data = {"title": title, "creator": creator, "isPartOf": id, "flavor": 'presentation/source'}
            body = {'body': fobj}
            url = self.url + '/ingest/addMediaPackage'
            #TODO: What if this fails?
            self._do_post(url, data=data, files=body, auth=auth)
 
    def _do_get(self, url):
        return requests.get(url, auth=HTTPDigestAuth(self.user, self.password),
                                headers={'X-Requestedresponse = -Auth': 'Digest'})

    def _do_post(self, url, data, files=None):
        return requests.post(url, data=data, files=files,
                      auth=HTTPDigestAuth(self.user, self.password), headers={'X-Requested-Auth': 'Digest'})



    def create_series(self, creator, title):

        metadata = [{"label": "Opencast Series DublinCore",
                     "flavor": "dublincore/series",
                     "fields": [{"id": "title",
                                 "value": title},
                                {"id": "creator",
                                 "value": [creator]}]}]
        #TODO: Load a default ACL template from somewhere
        acl = [{"allow": True,
                "action": "write",
                "role": "ROLE_AAI_USER_"+creator},
               {"allow": True,
                "action": "read",
                "role": "ROLE_AAI_USER_"+creator}]

        data = {"metadata": json.dumps(metadata),
                "acl": json.dumps(acl)}

        response = self._do_post(url+'/api/series', data=data)

        self.logger.debug(f"Creating series {title} get a {response.status_code} response")

        #What if response is something other than success?
        return json.loads(response.content.decode("utf-8"))["identifier"]
