import json
import requests
import os
import os.path
from requests.auth import HTTPDigestAuth
import sys
import configparser
from urllib.error import HTTPError
from zingest.rabbit import Rabbit
from zingest.zoom import Zoom
from zingest import db

import logging
import zingest.logger

class Opencast:

    IN_PROGRESS_ROOT = "in-progress"

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
        self.set_rabbit(rabbit)

    def set_rabbit(self, rabbit):
        if not rabbit or type(rabbit) != zingest.rabbit.Rabbit:
            raise TypeError("Rabbit is missing or the wrong type!")
        else:
            self.rabbit = rabbit

    def run(self):
        self.rabbit.start_consuming_rabbitmsg(self.rabbit_callback)


    def _do_download(self, url, output):
        with requests.get(url, stream=True) as req:
            #Raises an exception if there is one
            req.raise_for_status()
            with open(output, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    # If you have chunk encoded response uncomment if
                    # and set chunk_size parameter to None.
                    #if chunk:
                    f.write(chunk)
                f.close()
            r.close()


    def _do_get(self, url):
        return requests.get(url, auth=HTTPDigestAuth(self.user, self.password),
                                headers={'X-Requestedresponse = -Auth': 'Digest'})


    def _do_post(self, url, data, files=None):
        return requests.post(url, data=data, files=files,
                      auth=HTTPDigestAuth(self.user, self.password), headers={'X-Requested-Auth': 'Digest'})


    @db.with_session
    def rabbit_callback(dbs, self, method, properties, body):
        try:
            j = json.loads(body)

            try:
                uuid = j['uuid']
                rec = dbs.query(db.Recording).filter(db.Recording.uid == uuid)
                if None == rec:
                    self.logger.debug(f"{uuid} not found in db, creating new record")
                    #Create a database record so that we can recover if we're killed mid process
                    rec = db.Recording(j)
                    rec.update_status(db.Status.IN_PROGRESS)
                    dbs.merge(rec)
                    dbs.commit()
                else:
                    self.logger.debug(f"{uuid} found in db")
                    #return
            except Exception as dbe:
                self.logger.exception("Database error")
                #TODO: Push the message back into rabbit
                return

            self._process(j)

            try:
                #Create a database record so that we can recover if we're killed mid process
                rec.update_status(db.Status.FINISHED)
                dbs.merge(rec)
                dbs.commit()
            except Exception as dbe:
                self.logger.exception("Database error")
                #TODO: Now what? the message back into rabbit
                return
        except Exception as e:
            self.logger.exception("Processing exception")
            #We don't need to do anything here since we haven't marked the recording as FINISHED
            #It will get caught up in the next background processing run


    def _process(self, json):
            uuid = json['uuid']
            if not os.path.isdir(f'{self.IN_PROGRESS_ROOT}'):
                os.mkdir(f'{self.IN_PROGRESS_ROOT}')

            self.logger.info(f"Fetching {uuid}")
            try:
                data, fileid = self.fetch_file(json)
            except HTTPError as er:
                self.logger.exception("Unable to fetch file")
                return
            self.logger.info(f"Uploading {uuid} as {fileid}.mp4 to {self.url}")
            self.oc_upload(data["creator"], data["topic"], id)
            self._rm(f'{self.IN_PROGRESS_ROOT}/{fileid}.mp4')

    def _rm(self, path):
        self.logger.debug(f"Removing {path}")
        if os.path.isfile(path):
            os.remove(path)

    def fetch_file(self, body):
        #NB: Previously decoded here, unsure if needed
        data = body #json.loads(body)#.decode("utf-8"))
        if "recording_files" not in data:
            self.logger.error("No recording found")
            raise NoMp4Files("No recording found")
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
        self._do_download(f"{dl_url}/?access_token={data['token']}", f"{id}.mp4")
        self.logger.debug(f"Id is ${id}")
        return data, id


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
