# Copyright (c) 2020-2021 Couchbase, Inc All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging
import re
import glob
import json
import pathlib
import zipfile
import sys
import time
import os
from os import path

import util

COUCHBASE_LOG = 'couchbase.log'
STATS_SNAPSHOT_DIR_NAME = 'stats_snapshot'

class Source:
    """
    Represents a source of Couchbase stats data.
    """
    def __init__(self, port):
        self._port = port

    def short_name(self):
        """
        :return: a convenient short name for this source
        """
        return None

    def port(self):
        """
        Returns the port for the Prometheus (or Prometheus-like) instance that serves
        the stats associated with this source.
        :return: the port number
        """
        return self._port

    def host(self):
        """
        Returns the host on which this Prometheus-like instance that serves the stats
        associated with this source, runs.
        :return: host on which the stats server runs
        """
        pass

    def requires_auth(self):
        """
        :return: whether or not this stats source requires authentication
        """
        return False

    def basic_auth_user(self):
        """
        :return: the user to authenticate with or None if not required
        """
        return None

    def basic_auth_password(self):
        """
        :return: the password to authenticate with or None if not required
        """
        return None

    def stats_url_path(self):
        """
        :return: the URL path which along with the host and port should be used to
                 access the Prometheus-like API of the stats server
        """
        return ''

    def maybe_start(self, log_dir):
        """
        Starts the Prometheus-like instance that serves stats for this source if
        this source starts some stats server. If the server is already running, this
        method can be a no-op and in this case, it must return None.
        :param log_dir: the directory in which logs should be written
        :return: the process handle associated with the stats server or None
        """
        return None

    def get_buckets(self):
        """
        Returns the list of buckets associated with this stats Source
        :return: list of buckets
        """
        return []

    def get_min_and_max_times(self):
        """
        Returns a 2-tuple containing an estimate of the min and max POSIX timestamps
        times associated with this stats Source
        :return: 2-tuple (min time, max time)
        """
        return None

    @staticmethod
    def maybe_start_stats_servers(stats_sources, log_dir):
        nodes = []
        for stats_source in stats_sources:
            node = stats_source.maybe_start(log_dir)
            if node is not None:
                nodes.append(node)
        return nodes


class CBCollect(Source):
    """
    Represents a source of stats data that is a Prometheus instance running against
    the stats snapshot in a cbcollect.
    """
    def __init__(self, cbcollect_dir, short_name, prometheus_port):
        super(CBCollect, self).__init__(prometheus_port)
        self._short_name = short_name
        self._cbcollect_dir = cbcollect_dir
        self._config = None

    def short_name(self):
        return self._short_name

    def host(self):
        return '127.0.0.1'

    def maybe_start(self, log_dir):
        """
        Starts the Prometheus instance that serves stats for this source.
        """
        log_path = path.join(log_dir, 'prom-{}.log'.format(self._short_name))
        listen_addr = '0.0.0.0:{}'.format(self.port())
        args = [Source.PROMETHEUS_BIN,
                '--config.file', path.join(util.get_root_dir(), 'noscrape.yml'),
                '--storage.tsdb.path', path.join(self._cbcollect_dir, 'stats_snapshot'),
                '--storage.tsdb.no-lockfile',
                '--storage.tsdb.retention.time', '10y',
                '--query.lookback-delta', '600s',
                '--web.listen-address', listen_addr]
        logging.info('starting prometheus server on {} against {}; logging to {}'
                     .format(listen_addr,
                             path.join(self._cbcollect_dir, 'stats_snapshot'),
                             log_path))
        return util.start_process(args, log_path)

    def get_buckets(self):
        """
        Returns the list of buckets associated with this stats Source
        :return: list of buckets
        """
        if self._config is None:
            self._config = parse_couchbase_log(self._cbcollect_dir)
        return self._config['buckets']

    def get_min_and_max_times(self):
        """
        Returns a 2-tuple containing an estimate of the min and max POSIX timestamps
        times associated with this stats Source
        :return: 2-tuple (min time, max time)
        """
        return get_prometheus_times(self._cbcollect_dir)

    def make_snapshot_dir_path(candidate_cbcollect_dir):
        """
        Returns a path representing the 'stats_snapshot' directory in
        candidate_cbcollect_dir.
        :type candidate_cbcollect_dir: pathlib.Path
        :rtype: pathlib.Path
        """
        return candidate_cbcollect_dir / '{}'.format(STATS_SNAPSHOT_DIR_NAME)

    @staticmethod
    def snapshot_dir_exists(candidate_cbcollect_dir):
        """
        Returns whether or not the 'stats_snapshot' directory inside
        candidate_cbcollect_dir exists.
        :type candidate_cbcollect_dir: ,lib.Path
        """
        return CBCollect.make_snapshot_dir_path(candidate_cbcollect_dir).exists()

    @staticmethod
    def is_cbcollect_dir(candidate_path):
        """
        Returns a guess as to whether candidate_path represents a
        cbcollect directory by checking whether the 'stats_snapshot' directory exists
        inside it.
        :type candidate_path: pathlib.Path
        """
        return candidate_path.is_dir() and CBCollect.snapshot_dir_exists(candidate_path)

    @staticmethod
    def find_cbcollect_dirs():
        cbcollects = sorted(glob.glob('cbcollect_info*'))
        return [f for f in cbcollects if CBCollect.is_cbcollect_dir(pathlib.Path(f))]

    @staticmethod
    def get_stats_sources():
        result = []
        cbcollects = CBCollect.get_cbcollect_dirs()
        if len(cbcollects) == 0:
            if os.path.isdir(STATS_SNAPSHOT_DIR_NAME):
                # Found stats directory, assume we're inside an unzip'd cbcollect
                # directory
                cbcollects.append(".")
            else:
                logging.error('No "collectinfo*.zip" files or "cbcollect_info*" '
                              'directories or "{}" directory found'.format(
                    STATS_SNAPSHOT_DIR_NAME))
                sys.exit(1)
        data_source_names = get_data_source_names(cbcollects)
        for idx in range(len(cbcollects)):
            cbcollect_dir = cbcollects[idx]
            name = data_source_names[idx]
            source = cbstats.CBCollect(cbcollect_dir,
                                       name,
                                       prometheus_base_port + idx)
            stats_sources.append(source)

    @staticmethod
    def get_cbcollect_dirs():
        zips = sorted(glob.glob('*.zip'))
        for z in zips:
            with zipfile.ZipFile(z) as zip_file:
                CBCollect.maybe_extract_from_zipfile(zip_file)
        return CBCollect.find_cbcollect_dirs()

    @staticmethod
    def is_stats_snapshot_file(filename):
        """
        Returns whether filename contains 'stats_snapshot' (and thus is a file we
        probably want to extract from a cbcollect zip).
        :type filename: string
        :rtype: bool
        """
        return filename.find('/{}/'.format(STATS_SNAPSHOT_DIR_NAME)) >= 0

    @staticmethod
    def maybe_extract_from_zipfile(zip_file):
        """
        Extract files needed for Promtimer to run if necessary. Files needed by
        Promtimer are:
        * everything under the stats_snapshot directory; nothing is extracted if the
          stats_snapshot directory is already present
        * couchbase.log: extracted if not present
        """
        root = zipfile.Path(zip_file)
        for p in root.iterdir():
            if CBCollect.is_cbcollect_dir(p):
                snapshot_exists = CBCollect.snapshot_dir_exists(pathlib.Path(p.name))
                logging.debug("{}/stats_snapshot exists: {}".format(p.name,
                                                                    snapshot_exists))
                extracting = False
                for item in zip_file.infolist():
                    item_path = path.join(*item.filename.split('/'))
                    should_extract = False
                    if CBCollect.is_stats_snapshot_file(item.filename):
                        should_extract = not snapshot_exists
                    elif item.filename.endswith(COUCHBASE_LOG):
                        should_extract = not path.exists(item_path)
                    if should_extract:
                        logging.debug("zipfile item:{}, exists:{}".format(
                            item_path, path.exists(item_path)))
                        if not extracting:
                            extracting = True
                            logging.info('extracting stats, couchbase.log from cbcollect'
                                         ' zip:{}'
                                         .format(zip_file.filename))
                        zip_file.extract(item)

    @staticmethod
    def try_get_data_source_names(cbcollect_dirs, pattern, name_format):
        data_sources = []
        for cbcollect in cbcollect_dirs:
            m = re.match(pattern, cbcollect)
            name = cbcollect
            if m:
                name = name_format.format(*m.groups())
            data_sources.append(name)
        if len(set(data_sources)) == len(data_sources):
            return data_sources
        return None

    @staticmethod
    def get_data_source_names(cbcollect_dirs):
        """
        Returns a list of names of data sources for the given list of cbcollect directories.
        This function attempts to make short names uniquely identifying each cbcollect
        directory in the given list. Frequently the node name embedded in the name of
        the cbcollect directory will be sufficient to uniquely identify the directory,
        however it may be necessary to use the full name of the directory.
        :type cbcollect_dirs: list of names of directories, there should be no duplicate
                              entries
        :rtype: list of names of data sources; if cbcollect_dirs contains no duplicates the
                returned list is guaranteed to also contain no duplicates
        """
        regex = re.compile('cbcollect_info_ns_(\d+)\@(.*)_(\d+)-(\d+)')
        formats = ['{1}', 'ns_{0}@{1}', '{1}-{2}-{3}', 'ns_{0}-{1}-{2}-{3}']
        for fmt in formats:
            result = CBCollect.try_get_data_source_names(cbcollect_dirs, regex, fmt)
            if result:
                return result
        return cbcollect_dirs

    @staticmethod
    def get_stats_sources(base_port):
        result = []
        cbcollects = CBCollect.get_cbcollect_dirs()
        if len(cbcollects) == 0:
            if os.path.isdir(STATS_SNAPSHOT_DIR_NAME):
                # Found stats directory, assume we're inside an unzip'd cbcollect
                # directory
                cbcollects.append(".")
            else:
                logging.error('error: no "collectinfo*.zip" files or "cbcollect_info*" '
                              'directories or "{}" directory found'.format(
                    STATS_SNAPSHOT_DIR_NAME))
                return result
        data_source_names = CBCollect.get_data_source_names(cbcollects)
        for idx in range(len(cbcollects)):
            cbcollect_dir = cbcollects[idx]
            name = data_source_names[idx]
            source = CBCollect(cbcollect_dir,
                               name,
                               base_port + idx)
            result.append(source)
        return result

    @staticmethod
    def compute_min_and_max_times(sources):
        times = [s.get_min_and_max_times() for s in sources]
        return min([t[0] for t in times]), max([t[1] for t in times])


class ServerNode(Source):
    """
    Represents a source of stats data that is a running Couchbase Server node.
    """
    def __init__(self, cluster_host, cluster_port, user, password):
        super(ServerNode, self).__init__(cluster_port)
        self._cluster_host = cluster_host
        self._user = user
        self._password = password

    def short_name(self):
        return '{}:{}'.format(self._cluster_host, self.port())

    def host(self):
        return self._cluster_host

    def requires_auth(self):
        return True

    def basic_auth_user(self):
        return self._user

    def basic_auth_password(self):
        return self._password

    def stats_url_path(self):
        return '_prometheus'

    def maybe_start(self, log_dir):
        """
        Nothing to do as the server is assumed to be running.
        """
        return None

    def get_buckets(self):
        """
        Returns the list of buckets associated with this stats Source
        :return: list of buckets
        """
        response = util.execute_request('{}:{}'.format(self.host(), self.port()),
                                'pools/default/buckets',
                                username=self._user, password=self._password)
        bucket_list = json.loads(response.read())
        result = []
        for bucket in bucket_list:
            result.append(bucket['name'])
        return result

    def get_min_and_max_times(self):
        """
        Returns a 2-tuple containing an estimate of the min and max POSIX timestamps
        times associated with this stats Source
        :return: 2-tuple (min time, max time)
        """
        current = time.time()
        return current - 60 * 60, current

    def cluster_host(self):
        return self._cluster_host

    def user(self):
        return self._user

    def password(self):
        return self._password

    @staticmethod
    def get_stats_sources(cluster, user, password):
        result = []
        try:
            response = util.execute_request(cluster, 'pools/default/nodeServices',
                                            username=user, password=password)
            node_services = json.loads(response.read())
            for node in node_services['nodesExt']:
                host = node.get('hostname')
                if host is None:
                    host = '127.0.0.1'
                services = node['services']
                port = services['mgmt']
                source = ServerNode(host, port, user, password)
                result.append(source)
            return result
        except OSError as err:
            logging.error('error: can\'t access cluster: {}'.format(err))
            return []


def parse_couchbase_ns_config(cbcollect_dir):
    logging.debug('parsing couchbase.log (Couchbase config)')
    in_config = False
    in_buckets = False
    buckets = []
    section_divider_count = 0
    with open(path.join(cbcollect_dir, 'couchbase.log'), "r") as file:
        for full_line in file:
            line = full_line.rstrip()
            config_line = 'Couchbase config'
            if not in_config and line.rstrip() == config_line:
                in_config = True
            elif in_config:
                if line.strip().startswith('=================='):
                    section_divider_count += 1
                    if section_divider_count == 2:
                        break
                if not in_buckets and line == ' {buckets,':
                    in_buckets = True
                elif in_buckets:
                    if re.match('^ \{.*,$', line):
                        break
                    else:
                        m = re.match('^    [ \[]\{\"(.*)\",$', line)
                        if m:
                            bucket = m.groups()[0]
                            logging.debug('found bucket:{}'.format(bucket))
                            buckets.append(bucket)
    return {'buckets': sorted(buckets)}

def parse_couchbase_chronicle_older_version(cbcollect_dir):
    logging.debug('parsing couchbase.log (Chronicle config)')
    in_config = False
    in_buckets = False
    bucket_list = ''
    with open(path.join(cbcollect_dir, 'couchbase.log'), 'r') as file:
        for full_line in file:
            line = full_line.rstrip()
            if not in_config and line == 'Chronicle config':
                in_config = True
            elif in_config:
                # Names of bucket can be on a single or multiple lines
                end_of_list = False
                possible_buckets = ''
                if not in_buckets:
                    if line.startswith(' {bucket_names,'):
                        in_buckets = True
                        possible_buckets = line.replace(' {bucket_names,[', '')
                elif in_buckets:
                    possible_buckets = line

                if possible_buckets != '':
                    if possible_buckets.endswith(']},'):
                        possible_buckets = possible_buckets[:-3]
                        end_of_list = True

                    bucket_list += possible_buckets

                    if end_of_list:
                        break

    buckets = []
    if bucket_list != '':
        for b in bucket_list.replace(' ','').replace('"','').split(','):
            buckets.append(b)

    return {'buckets': sorted(buckets)}

def parse_couchbase_chronicle(cbcollect_dir):
    logging.debug('parsing couchbase.log (Chronicle config)')
    in_config = False
    in_buckets = False
    bucket_list = ''
    with open(path.join(cbcollect_dir, 'couchbase.log'), 'r') as file:
        for full_line in file:
            line = full_line.rstrip()
            if not in_config and line == 'Chronicle dump':
                in_config = True
            elif in_config:
                # Names of bucket can be on a single or multiple lines
                bucket_list = ''
                possible_buckets = ''
                if not in_buckets:
                    m = re.match('(^\s*{bucket_names,{\[)(.*)', line)
                    if m:
                        in_buckets = True
                        possible_buckets = m.group(2)
                elif in_buckets:
                    possible_buckets = line
                if possible_buckets != '':
                    m = re.match('^([^\]]*)\].*', possible_buckets)
                    if m:
                        bucket_list += m.group(1)
                        break
                    bucket_list += possible_buckets
    buckets = []
    if bucket_list != '':
        for b in bucket_list.replace(' ','').replace('"','').split(','):
            buckets.append(b)
    logging.debug('found buckets:{}'.format(buckets))
    return {'buckets': sorted(buckets)}

def parse_couchbase_log(cbcollect_dir):
    config = parse_couchbase_chronicle(cbcollect_dir)
    if config['buckets'] == []:
        config = parse_couchbase_chronicle_older_version(cbcollect_dir)
        if config['buckets'] == []:
            config = parse_couchbase_ns_config(cbcollect_dir)
    return config

def get_prometheus_times(cbcollect_dir):
    min_times = []
    max_times = []
    meta_files = glob.glob(path.join(cbcollect_dir, 'stats_snapshot', '*', 'meta.json'))
    for meta_file in meta_files:
        with open(meta_file, 'r') as file:
            meta = json.loads(file.read())
            min_times.append(meta['minTime'] / 1000.0)
            max_times.append(meta['maxTime'] / 1000.0)
    return min(min_times), max(max_times)
