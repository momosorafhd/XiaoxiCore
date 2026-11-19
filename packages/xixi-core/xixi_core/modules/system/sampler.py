"""Read-only Linux telemetry."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import os
import platform
import sys
import threading
import time
from .files import open_regular


class SnapshotBusy(Exception):
    pass


def cpu_counters(raw):
    result = {}
    for line in raw.splitlines()[:1025]:
        fields = line.split()
        if not fields or not re.fullmatch(r'cpu\d*', fields[0]):
            continue
        try:
            numbers = [int(n) for n in fields[1:9]]
            if len(numbers) < 4 or any(n < 0 for n in numbers):
                continue
            result[fields[0]] = (sum(numbers), numbers[3] + (numbers[4] if len(numbers) > 4 else 0))
        except ValueError:
            continue
    return result


def cpu_delta(before, after):
    def usage(key):
        if key not in before or key not in after:
            return None
        total, idle = (after[key][i] - before[key][i] for i in (0, 1))
        if total <= 0 or idle < 0 or idle > total:
            return None
        return round(100 * (total - idle) / total, 1)
    cores = [{'id':key, 'percent':usage(key)} for key in after if key != 'cpu']
    return {'status':'ok' if usage('cpu') is not None else 'unavailable',
            'percent':usage('cpu'), 'logical_cores':len(cores), 'cores':cores}


def memory_info(raw):
    values = {}
    for line in raw.splitlines()[:256]:
        match = re.fullmatch(r'([A-Za-z_()]+):\s*(\d+)\s+kB\s*', line)
        if match:
            values[match[1]] = int(match[2]) * 1024
    total = values.get('MemTotal', 0)
    if total <= 0:
        return {'status':'unavailable'}
    available = values.get('MemAvailable')
    estimated = available is None
    if estimated:
        available = sum(values.get(k,0) for k in ('MemFree','Buffers','Cached','SReclaimable')) - values.get('Shmem',0)
    available = min(total, max(0, available))
    used = total - available
    swap_total = values.get('SwapTotal',0)
    swap_used = max(0, swap_total - min(swap_total, values.get('SwapFree',swap_total)))
    return {'status':'ok', 'total_bytes':total, 'available_bytes':available,
            'used_bytes':used, 'percent':round(100 * used / total,1),
            'available_estimated':estimated, 'cached_bytes':max(0,values.get('Cached',0)),
            'free_bytes':values.get('MemFree'), 'buffers_bytes':values.get('Buffers'),
            'slab_bytes':values.get('Slab'), 'committed_bytes':values.get('Committed_AS'),
            'commit_limit_bytes':values.get('CommitLimit'),
            'swap_total_bytes':swap_total, 'swap_used_bytes':swap_used}


class SystemSampler:
    def __init__(self, proc_root='/proc', sys_root='/sys', *, enabled=None, sleep=time.sleep,
                 monotonic=time.monotonic, epoch=time.time, window=0.15,
                 mode_file='/etc/default/xixi-cpu-mode', cache_seconds=5, lock_timeout=1.0):
        self.proc, self.sys = Path(proc_root).resolve(), Path(sys_root).resolve()
        self.enabled = sys.platform.startswith('linux') if enabled is None else enabled
        self.sleep, self.clock, self.epoch, self.window = sleep, monotonic, epoch, window
        self.lock = threading.Lock()
        self.cached, self.cached_at = None, float('-inf')
        self.previous, self.previous_at = None, None
        self.disks_cached, self.disks_at = [], float('-inf')
        self.hardware_cached, self.hardware_at = {}, float('-inf')
        self.pci_labels = None
        self.cpu_static = None
        self.mode_file = Path(mode_file)
        self.cache_seconds = cache_seconds
        self.lock_timeout = lock_timeout

    def power_mode(self):
        selected=None
        try:
            with open_regular(self.mode_file) as stream:
                value=stream.read(129).decode('ascii').strip()
            selected=value if value in {'powersave','performance','restore'} else None
        except Exception:
            pass
        policies=[]
        for path in sorted((self.sys/'devices/system/cpu/cpufreq').glob('policy*'))[:1024]:
            policies.append({'maximum':self._unsigned(path/'scaling_max_freq'),
                'hardware_maximum':self._unsigned(path/'cpuinfo_max_freq'),
                'governor':self._label(self._read(path/'scaling_governor',128),'未公开')})
        turbo=self._unsigned(self.sys/'devices/system/cpu/intel_pstate/no_turbo')
        known=bool(policies) and all(p['maximum'] is not None and p['hardware_maximum'] is not None for p in policies)
        consistent=None
        if known and selected=='powersave':
            consistent=all(p['maximum']==min(p['hardware_maximum'],1800000) for p in policies) and turbo in (None,1)
        elif known and selected=='performance':
            consistent=all(p['maximum']==p['hardware_maximum'] for p in policies) and turbo in (None,0)
        label={'powersave':'省电模式','performance':'高性能模式','restore':'原始设置'}.get(selected,'模式未确认')
        if consistent is False:label+=' · 配置不一致'
        elif selected in {'powersave','performance'} and consistent is None:label+=' · 未核实'
        return {'selected':selected,'label':label,'consistent':consistent,
            'turbo_enabled':turbo==0 if turbo in (0,1) else None,
            'maximum_frequency_mhz':max((p['maximum'] for p in policies if p['maximum'] is not None),default=0)/1000 or None,
            'governors':sorted({p['governor'] for p in policies})}

    def pci_name(self, vendor, device):
        if self.pci_labels is None:
            self.pci_labels = {}
            for source in ('/usr/share/misc/pci.ids','/usr/share/hwdata/pci.ids'):
                try:
                    # Static distribution database, independent of GPU power.
                    with open(source,encoding='utf-8',errors='replace') as stream:
                        raw=stream.read(4*1024*1024)
                    current=None
                    for line in raw.splitlines():
                        match=re.match(r'^([0-9a-f]{4})  (.+)',line)
                        if match:
                            current=match[1]
                        else:
                            match=re.match(r'^\t([0-9a-f]{4})  (.+)',line)
                            if match and current:
                                self.pci_labels[current+':'+match[1]]=self._label(match[2],'显卡')
                    break
                except OSError:
                    continue
        return self.pci_labels.get(vendor.removeprefix('0x')+':'+device.removeprefix('0x'))

    def disks(self):
        if self.clock() - self.disks_at < 60:
            return self.disks_cached
        items, seen = [], set()
        for line in (self._read(self.proc/'self/mountinfo') or '').splitlines():
            fields = line.split()
            try:
                split = fields.index('-')
                device, mount = fields[2], fields[4]
                source = fields[split+2]
                if not source.startswith('/dev/') or source.startswith('/dev/loop') or device in seen:
                    continue
                mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1],8)), mount)
                usage = os.statvfs(mount)
                total = usage.f_blocks * usage.f_frsize
                if total <= 0:
                    continue
                used = (usage.f_blocks - usage.f_bfree) * usage.f_frsize
                items.append({'id':device, 'mount':mount, 'device':source,
                    'total_bytes':total, 'used_bytes':used,
                    'available_bytes':usage.f_bavail*usage.f_frsize,
                    'percent':round(100*used/total,1), 'status':'ok'})
                seen.add(device)
            except (ValueError, IndexError, OSError, AttributeError):
                continue
        self.disks_cached, self.disks_at = items[:32], self.clock()
        return self.disks_cached

    def hardware(self):
        if self.clock() - self.hardware_at < 60:
            return self.hardware_cached
        if self.cpu_static is None:
            records=[]
            for block in (self._read(self.proc/'cpuinfo') or '').split('\n\n'):
                record={}
                for line in block.splitlines():
                    if ':' in line:
                        key,value=(v.strip() for v in line.split(':',1));record[key]=value
                if record:records.append(record)
            first=records[0] if records else {}
            sockets={r['physical id'] for r in records if 'physical id' in r}
            cores={(r['physical id'],r['core id']) for r in records if 'physical id' in r and 'core id' in r}
            flags=set(first.get('flags','').split())
            cpu={'model':self._label(first.get('model name'),'处理器'),'architecture':platform.machine(),
                 'sockets':len(sockets) or None,'physical_cores':len(cores) or None,
                 'virtualization':'Intel VT-x（支持）' if 'vmx' in flags else 'AMD-V（支持）' if 'svm' in flags else None,
                 'address_sizes':first.get('address sizes'),'caches':[]}
            for name,key in [('base_frequency','base_frequency_mhz'),('cpuinfo_max_freq','maximum_frequency_mhz')]:
                value=self._unsigned(self.sys/'devices/system/cpu/cpu0/cpufreq'/name)
                cpu[key]=value/1000 if value is not None else None
            seen=set();caches={}
            for processor in sorted((self.sys/'devices/system/cpu').glob('cpu[0-9]*'))[:1024]:
                for cache in sorted((processor/'cache').glob('index[0-9]*'))[:16]:
                    level=(self._read(cache/'level',32) or '').strip()
                    kind=(self._read(cache/'type',32) or '').strip()
                    shared=(self._read(cache/'shared_cpu_list',4096) or '').strip()
                    size=(self._read(cache/'size',32) or '').strip()
                    match=re.fullmatch(r'(\d+)([KMG])',size)
                    identity=(level,kind,shared)
                    if level in ('1','2','3','4') and shared and match and identity not in seen:
                        seen.add(identity);caches[level]=caches.get(level,0)+int(match[1])*1024**('KMG'.index(match[2])+1)
            cpu['caches']=[{'level':'L'+level,'bytes':value} for level,value in sorted(caches.items())]
            self.cpu_static=cpu
        cpu=self.cpu_static
        gpus = []
        for path in sorted((self.sys/'bus/pci/devices').glob('*'))[:1024]:
            if not (self._read(path/'class',128) or '').strip().startswith('0x03'):
                continue
            vendor = (self._read(path/'vendor',128) or '').strip()
            device = (self._read(path/'device',128) or '').strip()
            driver = path/'driver'
            # IDs, driver symlink and runtime state are kernel metadata. Never
            # open PCI config/resource, poll GPU counters or call vendor tools.
            gpus.append({'id':path.name, 'vendor':{'0x8086':'Intel','0x10de':'NVIDIA','0x1002':'AMD'}.get(vendor,vendor),
                'name':self.pci_name(vendor,device),
                'kind':'integrated' if vendor=='0x8086' and device=='0x0416' else 'discrete' if vendor=='0x10de' else 'unknown',
                'pci_location':path.name,
                'revision':(self._read(path/'revision',128) or '').strip() or None,
                'subsystem_id':':'.join((self._read(path/name,128) or '').strip().removeprefix('0x') for name in ('subsystem_vendor','subsystem_device')).strip(':') or None,
                'kernel_version':platform.release(),
                'driver_version':self._label(self._read(driver/'module/version',256),'') or None,
                'device_id':vendor.removeprefix('0x')+':'+device.removeprefix('0x'),
                'driver':driver.resolve().name if driver.exists() else None,
                'status':'offline', 'reason':'suspended' if self._sleeping(path) else 'metrics_unavailable'})
        self.hardware_cached, self.hardware_at = {'cpu':cpu,'gpus':gpus}, self.clock()
        return self.hardware_cached

    def _unsigned(self,path):
        raw=self._read(path,128)
        try:
            value=int(raw.strip())
            return value if 0<=value<=2**64 else None
        except (AttributeError,ValueError):
            return None

    def gpus(self,hardware):
        result=[]
        for info in hardware.get('gpus',[]):
            path=self.sys/'bus/pci/devices'/info['id']
            state=(self._read(path/'power/runtime_status',128) or '').strip()
            sleeping=state!='active' or self._sleeping(path)
            # Never probe dynamic attributes on suspended devices.
            busy=None if sleeping else self._unsigned(path/'gpu_busy_percent')
            busy=busy if busy is not None and busy<=100 else None
            total=None if sleeping else self._unsigned(path/'mem_info_vram_total')
            used=None if sleeping else self._unsigned(path/'mem_info_vram_used')
            if total is not None and used is not None and used>total:
                used=None
            watts,limit=None,None
            if not sleeping:
                for hwmon in sorted((path/'hwmon').glob('hwmon[0-9]*'))[:16]:
                    power=self._unsigned(hwmon/'power1_average')
                    if power is None:power=self._unsigned(hwmon/'power1_input')
                    cap=self._unsigned(hwmon/'power1_cap') or self._unsigned(hwmon/'power1_cap_max')
                    if power is not None:watts=power/1000000
                    if cap:limit=cap/1000000
                    if watts is not None and limit is not None:break
            power_percent=round(100*watts/limit,1) if watts is not None and limit else None
            displayed=busy if busy is not None else power_percent
            result.append({**info,'status':'ok' if displayed is not None else 'offline' if sleeping else 'unavailable',
                'power_state':state or 'unknown',
                'reason':'suspended' if state in {'suspended','suspending'} else 'power_state_unknown' if sleeping else 'metrics_unavailable' if displayed is None else None,
                'percent':busy,'display_percent':displayed,'metric_kind':'utilization' if busy is not None else 'power' if power_percent is not None else None,
                'power_watts':watts,'power_limit_watts':limit,'power_percent':power_percent,
                'vram_total_bytes':total,'vram_used_bytes':used})
        return sorted(result,key=lambda item:({'integrated':0,'unknown':1,'discrete':2}.get(item['kind'],1),item['id']))

    def fans(self):
        result=[]
        for monitor in sorted((self.sys/'class/hwmon').glob('hwmon[0-9]*'))[:64]:
            driver=self._label(self._read(monitor/'name',128),'风扇传感器')
            sleeping=self._sleeping(monitor/'device')
            for channel in sorted(monitor.glob('fan[0-9]*_input'))[:64]:
                stem=channel.name[:-6]
                value=None if sleeping else self._unsigned(channel)
                value=value if value is not None and value<=100000 else None
                result.append({'id':monitor.name+'-'+stem,'component':driver,
                    'label':self._label(self._read(monitor/(stem+'_label'),128),stem.upper()),
                    'rpm':value,'status':'sleeping' if sleeping else 'ok' if value is not None else 'unavailable',
                    'min_rpm':None if sleeping else self._unsigned(monitor/(stem+'_min')),
                    'max_rpm':None if sleeping else self._unsigned(monitor/(stem+'_max'))})
        return result[:128]

    def _read(self, path, limit=65536):
        try:
            resolved = path.resolve()
            if not any(resolved.is_relative_to(root) for root in (self.proc, self.sys)):
                return None
            with resolved.open('rb') as stream:
                data = stream.read(limit + 1)
            return data.decode('utf-8', 'replace') if len(data) <= limit else None
        except (OSError, ValueError, RuntimeError):
            return None

    @staticmethod
    def _label(raw, fallback):
        text = ''.join(c for c in (raw or '').strip() if c.isprintable())[:80]
        return text or fallback

    def _temperature(self, path):
        raw = self._read(path,128)
        try:
            value = int(raw.strip()) / 1000
            return round(value,1) if -40 <= value <= 160 else None
        except (AttributeError, ValueError):
            return None

    def _sleeping(self, device):
        try:
            location = device.resolve()
            if not location.is_relative_to(self.sys):
                return True
            for parent in (location, *location.parents):
                if parent == self.sys:
                    break
                state = self._read(parent/'power/runtime_status',128)
                if state and state.strip() in {'suspended','suspending','error'}:
                    return True
        except (OSError, RuntimeError):
            return True
        return False

    def temperatures(self):
        items, seen = [], set()
        try:
            monitors = sorted((self.sys/'class/hwmon').glob('hwmon[0-9]*'))[:64]
            zones = sorted((self.sys/'class/thermal').glob('thermal_zone[0-9]*'))[:128]
        except OSError:
            return []
        for monitor in monitors:
            driver = self._label(self._read(monitor/'name',128), '温度传感器')
            sleeping = self._sleeping(monitor/'device')
            try:
                channels = sorted(monitor.glob('temp[0-9]*_input'))[:64]
            except OSError:
                continue
            for channel in channels:
                if len(items) >= 256:
                    break
                stem = channel.name[:-6]
                identity = str(channel.resolve())
                if identity in seen:
                    continue
                seen.add(identity)
                label = self._label(self._read(monitor/(stem+'_label'),128), stem.replace('temp','通道 '))
                value = None if sleeping else self._temperature(channel)
                items.append({'id':monitor.name+'-'+stem, 'component':driver, 'label':label,
                    'celsius':value, 'status':'sleeping' if sleeping else 'ok' if value is not None else 'unavailable',
                    'max_celsius':None if sleeping else self._temperature(monitor/(stem+'_max')),
                    'critical_celsius':None if sleeping else self._temperature(monitor/(stem+'_crit'))})
        for zone in zones:
            if len(items) >= 256:
                break
            zone_type = self._label(self._read(zone/'type',128),zone.name)
            drivers = {item['component'] for item in items}
            if zone_type in drivers or zone_type == 'x86_pkg_temp' and 'coretemp' in drivers:
                continue
            value = self._temperature(zone/'temp')
            if value is None:
                continue
            items.append({'id':zone.name, 'component':'ACPI / 热区',
                'label':zone_type, 'celsius':value,
                'status':'ok', 'max_celsius':None, 'critical_celsius':None})
        return items

    def collect(self):
        if not self.enabled:
            return {'status':'unavailable', 'reason':'linux_required', 'cpu':{'status':'unavailable'},
                    'memory':{'status':'unavailable'}, 'disks':[], 'gpus':[], 'temperatures':[], 'fans':[], 'uptime_seconds':None,
                    'collected_at':None, 'sample_window_ms':0, 'history':'none'}
        # A cold minute sample and a live request normally overlap for 150 ms.
        # Wait for that sample once, then the cache check below shares its result.
        # Long-running collection still has an explicit busy boundary.
        if not self.lock.acquire(timeout=self.lock_timeout):
            raise SnapshotBusy()
        try:
            if self.cached is not None and self.clock() - self.cached_at < self.cache_seconds:
                return self.cached
            start = self.clock()
            current = cpu_counters(self._read(self.proc/'stat') or '')
            interval = start - self.previous_at if self.previous_at is not None else 0
            if current and self.previous and 0.4 <= interval <= 2.5:
                cpu = cpu_delta(self.previous, current)
                sample_ms = round(interval * 1000)
            else:
                before = current
                if before:
                    self.sleep(self.window)
                current = cpu_counters(self._read(self.proc/'stat') or '')
                cpu = cpu_delta(before, current)
                sample_ms = round(1000 * (self.clock() - start))
            self.previous, self.previous_at = current, self.clock()
            memory = memory_info(self._read(self.proc/'meminfo') or '')
            raw_uptime = self._read(self.proc/'uptime',128)
            try:
                uptime = max(0,int(float(raw_uptime.split()[0])))
            except (AttributeError, ValueError, OverflowError, IndexError):
                uptime = None
            hardware = self.hardware()
            cpu.update(hardware.get('cpu',{}))
            frequency=self._unsigned(self.sys/'devices/system/cpu/cpu0/cpufreq/scaling_cur_freq')
            cpu['current_frequency_mhz']=frequency/1000 if frequency is not None else None
            cpu['power_mode']=self.power_mode()
            self.cached = {'status':'ok' if cpu['status']=='ok' or memory['status']=='ok' else 'unavailable',
                'cpu':cpu, 'memory':memory, 'temperatures':self.temperatures(), 'uptime_seconds':uptime,
                'disks':self.disks(), 'gpus':self.gpus(hardware), 'fans':self.fans(),
                'collected_at':datetime.fromtimestamp(self.epoch(),timezone.utc).isoformat(),
                'sample_window_ms':sample_ms, 'history':'none'}
            self.cached_at = self.clock()
            return self.cached
        finally:
            self.lock.release()
