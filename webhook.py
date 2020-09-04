import json
import configparser
import sys

from pprint import pformat

from markupsafe import escape
from flask import Flask, request, render_template, redirect, url_for
import logging
import zingest.logger
from zingest.rabbit import Rabbit
from zingest.zoom import Zoom
from zingest.opencast import Opencast

MIN_DURATION = 0

logger = logging.getLogger("webhook")
logger.info("Startup")

try:
    config = configparser.ConfigParser()
    config.read('settings.ini')
except FileNotFoundError:
    sys.exit("No settings found")

try:
    if bool(config['logging']['debug']):
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")
    PORT_NUMBER = int(config["Webhook"]["Port"])
    logger.debug(f"Webhook port is {PORT_NUMBER}")
    HOST_NAME = config["Webhook"]["Url"]
    logger.debug(f"Hostname is {HOST_NAME}")
    MIN_DURATION = int(config["Webhook"]["Min_Duration"])
    logger.debug(f"Minimum duration is is {MIN_DURATION}")
except KeyError as err:
    sys.exit("Key {0} was not found".format(err))
except ValueError as err:
    sys.exit("Invalid value, integer expected : {0}".format(err))

z = Zoom(config)
r = Rabbit(config, z)
o = Opencast(config, r)

app = Flask(__name__)


@app.route('/recordings/<user_id>', methods=['GET'])
def do_list_recordings(user_id):
    #TODO: We only accept YYYY-MM-DD, validate this
    from_date = request.args.get('from', None)
    to_date = request.args.get('to', None)
    page_size = request.args.get('pg_size', None)

    renderable = z.get_user_recordings(user_id, from_date = from_date, to_date = to_date, page_size = page_size)
    return render_template("recordings.html", recordings=renderable, user=user_id)


@app.route('/recording/<recording_id>', methods=['GET', 'POST'])
def single_recording(recording_id):
    if request.method == "GET":
        series_id = request.args.get("sid", None)
        return get_single_recording(recording_id, series_id)
    elif request.method == "POST":
        return ingest_single_recording(recording_id)


def get_single_recording(recording_id, series_id = None, workflow_id = None):
    renderable = z.get_recording(recording_id)
    series = None
    if series_id:
        series = o.get_single_series(series_id)
    return render_template("ingest.html", recording=renderable, workflow_list = o.get_workflows(), series_list = o.get_series(), series = series, workflow = workflow_id)


def ingest_single_recording(recording_id):
    logger.debug(f"Post for { recording_id }")
    for key in request.form.keys():
        logger.debug(f"{ key } = { request.form[key] }")
    user_id = request.form['origin_email']
    return redirect(f'/recordings/{ user_id }')


@app.route('/series', defaults={'series_id': None}, methods=['GET', 'POST'])
@app.route('/series/<series_id>', methods=['GET', 'POST']) #FIXME: The GET here only partially renders correctly, POST should be PUT to reflect OC api use of PUT for modifying existing series
def get_series_list(series_id=None):
    if request.method == "GET":
        series = None
        if None != series_id:
            series = o.get_single_series(series_id)
            #TODO: Need to get the theme and acl data from the respective endpoints ({sid}/acl and {sid}/properties -> { 'theme': $id })
        epId = request.args.get('epid', "")
        return render_template("series.html", series = series, acl_list = o.get_acls(), theme_list = o.get_themes(), origin_epid = epId)
    elif request.method == "POST":
        for key in request.form.keys():
          logger.debug(f"{ key } = { request.form[key] }")
        epid = request.form['origin_epid']
        #Create the series
        o.create_series(**request.form)
        #Redirect either to the episode (epId) or back to the create series bits in case of error
        return redirect(f'recording/{ epid }?sid={ series_id }')


@app.route('/', methods=['GET'])
def do_GET():
    return "Hello World"


@app.route('/', methods=['POST'])
@app.errorhandler(400)
def do_POST():
    """Respond to Webhook"""
    logger.debug("POST recieved")
    content_length = int(request.headers.get('Content-Length'))
    if content_length < 5:
        logger.error("Content too short")
        return render_template_string("No data received", ""), 400

    #Check UTF8 safeness of this
    body = request.get_json(force=True)
    if "payload" not in body:
        logger.error("Payload is missing")
        return render_template_string("Missing payload field in webhook body", ""), 400

    return handle_webhook(body)


def handle_webhook(body):
    payload = body["payload"]
    try:
        z.validate_payload(payload)
    except BadWebhookData as e:
        logger.error("Payload failed validation")
        return render_template_string("Payload failed validation", ""), 400
    except NoMp4Files as e:
        logger.error("No mp4 files found!")
        return render_template_string("No mp4 files found!", ""), 400

    if payload["object"]["duration"] < MIN_DURATION:
        logger.error("Recording is too short")
        return render_template_string("Recording is too short", ""), 400

    token = body["download_token"]
    logger.debug(f"Token is {token}")

    logger.debug("Sending rabbit message")
    r.send_rabbit_msg(payload['object'], token)

    logger.debug("POST processed successfully")
    return "Success"

