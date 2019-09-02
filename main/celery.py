from django.conf import settings
import os
from celery import Celery
import tempfile
import json
from ohapi import api
import requests
import io
import bz2
import logging
import vcf
from .celery_helper import temp_join

# set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oh_data_uploader.settings')
OH_BASE_URL = settings.OPENHUMANS_OH_BASE_URL

logger = logging.getLogger(__name__)

app = Celery('proj')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object('django.conf:settings', namespace='CELERY')
app.conf.update(CELERY_BROKER_URL=os.environ['REDIS_URL'],
                CELERY_RESULT_BACKEND=os.environ['REDIS_URL'])

# Load task modules from all registered Django app configs.
app.autodiscover_tasks()
# app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)


def create_tempfile(dfile, suffix):
    tf_in = tempfile.NamedTemporaryFile(suffix="." + suffix)
    tf_in.write(requests.get(dfile['download_url']).content)
    tf_in.flush()
    return tf_in


def verify_vcf(dfile):
    """
    Verify that this is a VCF file.
    """
    base_name = dfile['basename']
    if base_name.endswith('.vcf.gz'):
        input_file = create_tempfile(dfile, ".vcf.gz")
        input_vcf = vcf.Reader(filename=input_file.name, compressed=True)
    elif base_name.endswith('.vcf.bz2'):
        input_file = create_tempfile(dfile, ".vcf.bz2")
        vcf_file = io.TextIOWrapper(bz2.BZ2File(input_file.name))
        input_vcf = vcf.Reader(vcf_file)
    elif base_name.endswith('.vcf'):
        input_file = create_tempfile(dfile, ".vcf")
        input_vcf = vcf.Reader(filename=input_file.name)
    else:
        raise ValueError("Input filename doesn't match .vcf, .vcf.gz, "
                         'or .vcf.bz2')

    # Check that it can advance one record without error.
    next(input_vcf)

    return input_vcf.metadata


def process_file(dfile, access_token, member, metadata):
    try:
        vcf_metadata = verify_vcf(dfile)
    except:
        api.message("VCF integration: A broken file was deleted",
                    "While processing your VCF file "
                    "we noticed that your file does not conform "
                    "to the expected specifications and it was "
                    "thus deleted. Email us as support@openhumans.org if "
                    "you think this file should be valid.",
                    access_token, base_url=OH_BASE_URL)
        api.delete_file(access_token,
                        str(member['project_member_id']),
                        file_id=str(dfile['id']),
                        base_url=OH_BASE_URL)
        raise
    try:
        tmp_directory = tempfile.mkdtemp()
        base_filename = dfile['basename']

        # Save raw 23andMe genotyping to temp file.
        if base_filename.endswith('.gz'):
            base_filename = base_filename[0:-3]
        elif base_filename.endswith('.bz2'):
            base_filename = base_filename[0:-4]
        meta_filename = base_filename + '.metadata.json'
        raw_filename = temp_join(tmp_directory, meta_filename)
        metadata = {
                    'description':
                    'VCF file metadata',
                    'tags': ['vcf']
                    }
        with open(raw_filename, 'w') as raw_file:
            json.dump(vcf_metadata, raw_file)
            raw_file.flush()

        api.upload_aws(raw_filename, metadata,
                       access_token, base_url=OH_BASE_URL,
                       project_member_id=str(member['project_member_id']))
    except:
        api.message("VCF integration: File could not be uploaded",
                    "Something went wrong when processing your "
                    "file. Please try to upload it again. "
                    "Please email us as support@openhumans.org if "
                    "this keeps happening.",
                    access_token, base_url=OH_BASE_URL)
        api.delete_file(access_token,
                        str(member['project_member_id']),
                        file_id=str(dfile['id']),
                        base_url=OH_BASE_URL)
        raise


@app.task(bind=True)
def clean_uploaded_file(self, access_token, file_id):
    member = api.exchange_oauth2_member(access_token, base_url=OH_BASE_URL)
    for dfile in member['data']:
        if dfile['id'] == file_id:
            process_file(dfile, access_token, member, dfile['metadata'])
    pass
