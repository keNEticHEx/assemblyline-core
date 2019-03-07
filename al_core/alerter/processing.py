import hashlib
import time

from assemblyline.common import forge
from assemblyline.common.classification import InvalidClassification
from assemblyline.common.caching import TimeExpiredCache
from assemblyline.common.dict_utils import recursive_update
from assemblyline.common.str_utils import safe_str
from assemblyline.common.isotime import now_as_iso
from assemblyline.odm.messages.alert import AlertMessage
from assemblyline.remote.datatypes.queues.comms import CommsQueue

CACHE_LEN = 60 * 60 * 24
CACHE_EXPIRY_RATE = 60

action_queue_map = None
cache = TimeExpiredCache(CACHE_LEN, CACHE_EXPIRY_RATE)
Classification = forge.get_classification()
config = forge.get_config()
summary_tags = (
    "AV_VIRUS_NAME", "EXPLOIT_NAME",
    "FILE_CONFIG", "FILE_OBFUSCATION", "FILE_SUMMARY",
    "IMPLANT_FAMILY", "IMPLANT_NAME", "NET_DOMAIN_NAME", "NET_IP",
    "TECHNIQUE_OBFUSCATION", "THREAT_ACTOR",
)

def get_submission_record(counter, datastore, sid):
    srecord = datastore.submission.get(sid, as_obj=False)

    if not srecord:
        counter.increment('alert.err_no_submission')
        time.sleep(1.0)
        raise Exception("Couldn't find submission: %s" % sid)

    if srecord.get('state', 'unknown') != 'completed':
        time.sleep(1.0)
        raise Exception("Submission not finalized: %s" % sid)

    return srecord


def get_summary(datastore, srecord):
    max_classification = srecord['classification']

    summary = {
        'AV_VIRUS_NAME': set(),
        'EXPLOIT_NAME': set(),
        'FILE_ATTRIBUTION': set(),
        'FILE_CONFIG': set(),
        'FILE_OBFUSCATION': set(),
        'FILE_SUMMARY': set(),
        'FILE_YARA_RULE': set(),
        'IMPLANT_FAMILY': set(),
        'IMPLANT_NAME': set(),
        'NET_DOMAIN_NAME_S': set(),
        'NET_DOMAIN_NAME_D': set(),
        'NET_IP_S': set(),
        'NET_IP_D': set(),
        'TECHNIQUE_CONFIG': set(),
        'TECHNIQUE_OBFUSCATION': set(),
        'THREAT_ACTOR': set()
    }

    for t in datastore.get_tag_list_from_keys(srecord.get('results', [])):
        tag_value = t['value']
        if tag_value == '':
            continue

        tag_context = t.get('context', None)
        tag_type = t['type']
        if tag_type in ('NET_DOMAIN_NAME', 'NET_IP'):
            if tag_context is None:
                tag_type += '_S'
            else:
                tag_type += '_D'
        elif tag_type not in summary:
            continue

        try:
            max_classification = Classification.max_classification(t['classification'], max_classification)
        except InvalidClassification:
            continue

        sub_tag = {
            'EXPLOIT_NAME': 'EXP',
            'FILE_CONFIG': 'CFG',
            'FILE_OBFUSCATION': 'OB',
            'IMPLANT_FAMILY': 'IMP',
            'IMPLANT_NAME': 'IMP',
            'TECHNIQUE_CONFIG': 'CFG',
            'TECHNIQUE_OBFUSCATION': 'OB',
            'THREAT_ACTOR': 'TA',
        }.get(tag_type, None)
        if sub_tag:
            tag_type = 'FILE_ATTRIBUTION'
            tag_value = "%s [%s]" % (tag_value, sub_tag)

        if tag_type == 'AV_VIRUS_NAME':
            if tag_value in (
                'Corrupted executable file',
                'Encrypted container deleted',
                'Encrypted container deleted;',
                'Password-protected',
                'Malformed container violation',
            ):
                tag_type = 'FILE_SUMMARY'

            else:
                av_name = (tag_context or '').split('scanner:')
                if len(av_name) == 2:
                    av_name = av_name[1]
                else:
                    av_name = datastore.service_name_from_key(t['key'])

                if av_name:
                    tag_value = "[%s] %s" % (av_name, tag_value)

        summary_values = summary.get(tag_type, None)
        if summary_values is not None:
            summary_values.add(tag_value)

    return max_classification, summary

def generate_alert_id(alert_data):
    parts = [
        alert_data['params']['psid'] or alert_data['sid'],
        alert_data['metadata'].get('ts', alert_data['ingest_time'])
    ]
    return hashlib.md5("-".join(parts).encode("utf-8")).hexdigest()


# noinspection PyBroadException
def parse_submission_record(counter, datastore, sid, psid, logger):
    srecord = get_submission_record(counter, datastore, sid)

    max_classification, summary = get_summary(datastore, srecord)
    summary_list = list(summary['FILE_SUMMARY'])

    extended_scan = 'unknown'
    if not psid:
        extended_scan = 'submitted' if psid is None else 'skipped'
    else:
        try:
            # Get errors from parent submission and submission. Strip keys
            # to only sha256 and service name. If there are any keys that
            # did not exist in the parent the extended scan is 'incomplete'.
            ps = datastore.submission.get(psid, as_obj=False) or {}
            pe = set((x[:x.rfind('.')] for x in ps.get('errors', [])))
            e = set((x[:x.rfind('.')] for x in srecord.get('errors', [])))
            ne = e.difference(pe)
            extended_scan = 'incomplete' if ne else 'completed'
        except Exception:  # pylint: disable=W0702
            logger.exception('Problem determining extended scan state:')

    domains = summary['NET_DOMAIN_NAME_D'].union(summary['NET_DOMAIN_NAME_S'])
    ips = summary['NET_IP_D'].union(summary['NET_IP_S'])

    return {
        'summary_list': summary_list,
        'extended_scan': extended_scan,
        'domains': domains,
        'ips': ips,
        'summary': summary,
        'srecord': srecord,
        'max_classification': max_classification
    }

AL_FIELDS = [
    'attrib',
    'av',
    'domain',
    'domain_dynamic',
    'domain_static',
    'ip',
    'ip_dynamic',
    'ip_static',
    'summary',
    'yara',
]
def perform_alert_update(datastore, logger, alert):
    alert_id = alert.get('alert_id')
    old_alert = datastore.alert.get(alert_id, as_obj=False) or {}

    # Merge fields...
    merged = {
        x: list(set(old_alert.get('al', {}).get(x, [])).union(set(alert['al'].get(x, [])))) for x in AL_FIELDS
    }

    # Sanity check.
    if not all([old_alert.get(x, None) == alert.get(x, None) for x in config.core.alerter.constant_alert_fields]):
        raise ValueError("Constant alert field changed. (%s, %s)" % (str(old_alert), str(alert)))

    old_alert = recursive_update(old_alert, alert)
    old_alert['al'] = recursive_update(old_alert['al'], merged)

    datastore.alert.save(alert_id, old_alert)
    logger.info(f"Alert {alert_id} has been updated.")


def save_alert(datastore, counter, logger, alert, psid):
    if psid:
        msg_type = "AlertUpdated"
        perform_alert_update(datastore, logger, alert)
        counter.increment('alert.updated')
    else:
        msg_type = "AlertCreated"
        datastore.alert.save(alert['alert_id'], alert)
        counter.increment('alert.saved')

    msg = AlertMessage({
        "msg": alert,
        "msg_type": msg_type,
        "sender": "alerter"
    })
    CommsQueue('alerts').publish(msg.as_primitives())


# noinspection PyTypeChecker
def get_alert_update_parts(counter, datastore, alert_data, logger):
    global cache
    # Check cache
    alert_update_p1, alert_update_p2 = cache.get(alert_data['sid'], (None, None))
    if alert_update_p1 is None or alert_update_p2 is None:
        parsed_record = parse_submission_record(counter, datastore, alert_data['sid'],
                                                alert_data['params']['psid'], logger)
        file_record = datastore.file.get(alert_data['sha256'], as_obj=False)
        alert_update_p1 = {
            'extended_scan': parsed_record['extended_scan'],
            'al': {
                'attrib': list(parsed_record['summary']['FILE_ATTRIBUTION']),
                'av': list(parsed_record['summary']['AV_VIRUS_NAME']),
                'domain': list(parsed_record['domains']),
                'domain_dynamic': list(parsed_record['summary']['NET_DOMAIN_NAME_D']),
                'domain_static': list(parsed_record['summary']['NET_DOMAIN_NAME_S']),
                'ip': list(parsed_record['ips']),
                'ip_dynamic': list(parsed_record['summary']['NET_IP_D']),
                'ip_static': list(parsed_record['summary']['NET_IP_S']),
                'request_end_time': parsed_record['srecord']['times']['completed'],
                'summary': parsed_record['summary_list'],
                'yara': list(parsed_record['summary']['FILE_YARA_RULE']),
            },

        }
        alert_update_p2 = {
            'classification': parsed_record['max_classification'],
            'file': {
                'md5': file_record['md5'],
                'sha1': file_record['sha1'],
                'sha256': file_record['sha256'],
                'size': file_record['size'],
                'type': file_record['type']
            }
        }
        cache.add(alert_data['sid'], (alert_update_p1, alert_update_p2))

    alert_update_p1['reporting_ts'] = now_as_iso()
    alert_update_p1['file'] = {'name': alert_data['filename']}

    return alert_update_p1, alert_update_p2


def process_alert_message(counter, datastore, logger, alert_data):
    """
    This is the default process_alert_message function. If the generic alerts are not sufficient
    in your deployment, you can create another method like this one that would follow
    the same structure but with added parts where the comment blocks are located.
    """

    ###############################
    # Additional init goes here
    ###############################

    alert = {
        'al': {
            'score': alert_data['score']
        },
        'alert_id': generate_alert_id(alert_data),
        'expiry_ts': now_as_iso(config.core.alerter.alert_ttl * 24 * 60 *60),
        'metadata': {safe_str(key): value for key, value in alert_data['metadata'].items()},
        'sid': alert_data['sid'],
        'ts': alert_data['ingest_time'],
        'type': alert_data['params']['type']
    }

    ###############################
    # Additional alert_data parsing
    # and alert updating goes here
    ###############################

    # Get update parts
    alert_update_p1, alert_update_p2 = get_alert_update_parts(counter, datastore, alert_data, logger)

    # Update alert with default values
    alert = recursive_update(alert, alert_update_p1)

    # Update alert with computed values
    alert = recursive_update(alert, alert_update_p2)

    save_alert(datastore, counter, logger, alert, alert_data['params']['psid'])
