import os
import json
import threading
import time
import re
import gzip

try:
    from urllib.parse import urlencode
    from urllib.request import urlopen, urlretrieve
except ImportError:
    from urllib import urlencode, urlretrieve
    from urllib2 import urlopen

'''
DATA_FILE obtained from the query:

URL: https://activedata.allizom.org/tools/query.html#query_id=ki_K7dxs

QUERY:
{
    "from":"treeherder",
    "limit":10000,
    "select":["job.details.url"],
    "where":{"and":[
        {"eq":{"build.branch":"mozilla-central"}},
        {"regex":{"job.type.name":".*android.*p2.*aarch64.*speedometer.*power.*"}},
        {"or":[
            {"eq":{"job.details.value":"batterystats.txt"}},
            {"eq":{"job.details.value":"battery-before.txt"}}
        ]}
    ]}
}


DATA_POINTS_FILE obtained from the query:

URL: https://activedata.allizom.org/tools/query.html#query_id=tVCw4S0T

QUERY:
{
    "from":"treeherder",
    "limit":10000,
    "select":["run.taskcluster.id"],
    "where":{"and":[
        {"eq":{"build.branch":"mozilla-central"}},
        {"regex":{"job.type.name":".*android.*p2.*aarch64.*speedometer.*power.*"}},
        {"or":[
            {"eq":{"job.details.value":"batterystats.txt"}},
            {"eq":{"job.details.value":"battery-before.txt"}}
        ]},
        {"gte":{"repo.push.date":"1558215900"}}
    ]}
}

'''

MAX_REQUESTS = 20
TOP_DIR = '/home/sparky/mozilla-source/screen_noise_investigation/'
DATA_FILE = os.path.join(TOP_DIR, '10000_screen_noise_data_points.json')
DATA_POINTS_FILE = os.path.join(TOP_DIR, 'good_data_points.json')
DOWNLOAD_DIR = os.path.join(TOP_DIR, 'downloads')
PRE_PROC_DIR = os.path.join(TOP_DIR, 'preproced')

if not os.path.exists(DOWNLOAD_DIR):
    os.mkdir(DOWNLOAD_DIR)
if not os.path.exists(PRE_PROC_DIR):
    os.mkdir(PRE_PROC_DIR)


def download_artifact(url, output_dir, ret_area, i):
    global current_reqs
    name = url.split('task')[-1]
    name = name.replace('/', ':')
    fname = os.path.join(output_dir, name)
    if os.path.exists(fname):
        print("Already exists: %s" % fname)
        ret_area[i][url] = fname
        current_reqs -= 1
        return fname
    print('Downloading ' + url + ' to: ' + fname)
    urlretrieve(url, fname)
    ret_area[i][url] = fname
    current_reqs -= 1
    return fname

def finalize_power_measurements(batterystats, binary, test_name, os_baseline=False):
    # Get the android version
    android_version = 8.0
    major_android_version = 8.0

    estimated_power = False
    uid = None
    total = cpu = wifi = smearing = screen = proportional = 0
    full_screen = 0
    full_wifi = 0
    re_uid = re.compile(r'proc=([^:]+):"%s"' % binary)
    re_wifi = re.compile(r'.*wifi=([\d.]+).*')
    re_cpu = re.compile(r'.*cpu=([\d.]+).*')
    re_estimated_power = re.compile(r"\s+Estimated power use [(]mAh[)]")
    re_proportional = re.compile(r"proportional=([\d.]+)")
    re_screen = re.compile(r"screen=([\d.]+)")
    re_full_screen = re.compile(r"\s+Screen:\s+([\d.]+)")
    re_full_wifi = re.compile(r"\s+Wifi:\s+([\d.]+)")

    re_smear = re.compile(r".*smearing:\s+([\d.]+)\s+.*")
    re_power = re.compile(
        r"\s+Uid\s+\w+[:]\s+([\d.]+) [(]([\s\w\d.\=]*)(?:([)] "
        r"Including smearing:.*)|(?:[)]))"
    )

    batterystats = batterystats.split("\n")
    for line in batterystats:
        if uid is None and not os_baseline:
            # The proc line containing the Uid and app name appears
            # before the Estimated power line.
            match = re_uid.search(line)
            if match:
                print("matched")
                uid = match.group(1)
                re_power = re.compile(
                    r"\s+Uid %s[:]\s+([\d.]+) [(]([\s\w\d.\=]*)(?:([)] "
                    r"Including smearing:.*)|(?:[)]))" % uid
                )
                continue
        if not estimated_power:
            # Do not attempt to parse data until we have seen
            # Estimated Power in the output.
            match = re_estimated_power.match(line)
            if match:
                estimated_power = True
            continue
        if full_screen == 0:
            match = re_full_screen.match(line)
            if match and match.group(1):
                full_screen += float(match.group(1))
                continue
        if full_wifi == 0:
            match = re_full_wifi.match(line)
            if match and match.group(1):
                full_wifi += float(match.group(1))
                continue
        if re_power:
            match = re_power.match(line)
            if match:
                ttotal, breakdown, smear_info = match.groups()
                total += float(ttotal) if ttotal else 0

                cpu_match = re_cpu.match(breakdown)
                if cpu_match and cpu_match.group(1):
                    cpu += float(cpu_match.group(1))

                wifi_match = re_wifi.match(breakdown)
                if wifi_match and wifi_match.group(1):
                    wifi += float(wifi_match.group(1))

                if smear_info:
                    # Smearing and screen power are only
                    # available on android 8+
                    smear_match = re_smear.match(smear_info)
                    if smear_match and smear_match.group(1):
                        smearing += float(smear_match.group(1))
                    screen_match = re_screen.search(line)
                    if screen_match and screen_match.group(1):
                        screen += float(screen_match.group(1))
                    prop_match = re_proportional.search(smear_info)
                    if prop_match and prop_match.group(1):
                        proportional += float(prop_match.group(1))
        if full_screen and full_wifi and (cpu and wifi and smearing or total):
            # Stop parsing batterystats once we have a full set of data.
            # If we are running an OS baseline, stop when we've exhausted
            # the list of entries.
            if not os_baseline:
                break
            elif line.replace(' ', '') == '':
                break

    cpu = total if cpu == 0 else cpu
    screen = full_screen if screen == 0 else screen
    wifi = full_wifi if wifi == 0 else wifi

    if os_baseline:
        uid = 'all'
    print(
        "power data for uid: %s, cpu: %s, wifi: %s, screen: %s, proportional: %s"
        % (uid, cpu, wifi, screen, proportional)
    )


    # Send power data directly to the control-server results handler
    # so it can be formatted and output for perfherder ingestion

    power_data = {
        "type": "power",
        "test": test_name,
        "unit": "mAh",
        "values": {
            "cpu": float(cpu),
            "wifi": float(wifi),
            "screen": float(screen),
        },
    }

    if major_android_version >= 8:
        power_data['values']['proportional'] = float(proportional)

    return power_data

limit = 999999
current_reqs = 0
reqs_locker = threading.Lock()

def main():
    global current_reqs
    with open(DATA_POINTS_FILE, 'r') as f:
        good_points = json.load(f)['data']['run.taskcluster.id']

    # Open data entries file
    with open(DATA_FILE, 'r') as f:
        data = json.load(f)['data']['job.details.url']

    formatted_data_for_download = {}
    for i, data_entry in enumerate(data):
        if i > limit:
            break
        if not data_entry:
            continue

        formatted_data_for_download[str(i)] = {}
        for entry in data_entry:
            if not entry:
                continue
            formatted_data_for_download[str(i)][entry] = None

    # Download all the data
    DONE = False
    threads = []
    for num in formatted_data_for_download:
        if int(num) > limit:
            break
        for entry in formatted_data_for_download[num]:
            first_entry = entry.split('task')[-1]
            task_id = first_entry.split('/')[1]
            if task_id not in good_points:
                print("Skipping task ID %s" % task_id)
                continue

            current_reqs += 1
            t = threading.Thread(
                target=download_artifact,
                args=(entry, DOWNLOAD_DIR, formatted_data_for_download, num)
            )
            t.start()
            threads.append(t)

            while current_reqs >= MAX_REQUESTS:
                time.sleep(1)
                print("Waiting for requests to finish, currently at %s" % str(current_reqs))

    for t in threads:
        t.join()

    formatted_data_for_download = {
        num: data
        for num, data in formatted_data_for_download.items()
        if all([data[key] for key in data])
    }

    print(formatted_data_for_download)

    # Pre-process the data into something manageable (save proced entries)
    pre_proced_data = {}
    re_temperature = re.compile(r""".*\s+temperature:\s+([\d.]+)""")
    for num in formatted_data_for_download:
        pre_proced_data[num] = {}
        for url, fname in formatted_data_for_download[num].items():
            _, file = os.path.split(fname)
            pre_proc_file = os.path.join(PRE_PROC_DIR, file)
            if os.path.exists(pre_proc_file):
                print("Already processed %s" % fname)
                with open(pre_proc_file, 'r') as f:
                    data = f.read().strip()
                    if 'battery-before' in pre_proc_file:
                        pre_proced_data[num]['temp'] = float(data)
                    else:
                        pre_proced_data[num]['screen'] = float(data)
                continue
            if 'battery-before.txt' in fname:
                try:
                    with open(fname, 'r') as f:
                        bat_stats = f.read()
                except Exception as e:
                    with open(fname, 'rb') as f:
                        gzip_fd = gzip.GzipFile(fileobj=f)
                        bat_stats = str(gzip_fd.read())

                temperature = None
                match = re_temperature.search(bat_stats)
                if match:
                    temperature = float(match.group(1))
                else:
                    print('\nFailed on file %s' % fname)
                    print('Skipping grouping number %s...\n' % num)
                    pre_proced_data[num] = {}
                    break

                with open(pre_proc_file, 'w') as f:
                    f.write(str(temperature))
                pre_proced_data[num]['temp'] = str(temperature)

            elif 'batterystats.txt' in fname:
                batterystats = None
                try:
                    with open(fname, 'r') as f:
                        batterystats = f.read()
                except Exception as e:
                    with open(fname, 'rb') as f:
                        gzip_fd = gzip.GzipFile(fileobj=f)
                        batterystats = str(gzip_fd.read())
                if not batterystats:
                    print('\nFailed on file %s' % fname)
                    print('Skipping grouping number %s...\n' % num)
                    pre_proced_data[num] = {}
                    break
                test_dir = os.path.join(TOP_DIR, 'testing')
                if not os.path.exists(test_dir):
                    os.mkdir(test_dir)
                with open(os.path.join(test_dir, file), 'w') as f:
                    f.write(batterystats)

                batterystats = batterystats.replace('\\n', '\n')
                power_data = finalize_power_measurements(batterystats, "org.mozilla.geckoview_example", 'test-name')
                screen = power_data['values']['screen']
                with open(pre_proc_file, 'w') as f:
                    f.write(str(screen))
                pre_proced_data[num]['screen'] = screen

            else:
                print("Unknown file name: %s" % fname)

    print(pre_proced_data)
    pre_proced_data = {
        num: data
        for num, data in pre_proced_data.items()
        if data and len(data.keys()) >= 2
    }

    print(len(pre_proced_data.keys()))

    from matplotlib import pyplot as plt

    xs = []; ys = []
    for num in pre_proced_data:
        if pre_proced_data[num]['screen'] > 20 or \
           pre_proced_data[num]['screen'] <= 0:
            continue
        xs.append(pre_proced_data[num]['temp'])
        ys.append(pre_proced_data[num]['screen'])

    plt.figure()
    plt.scatter(ys,xs)
    plt.title("Screen Power vs. Battery Temperature")
    plt.ylabel("Temperature")
    plt.xlabel("Screen Power")
    plt.show()


    return

if __name__=="__main__":
    main()