#!/usr/bin/env python3
from pathlib import Path


membership = Path('/proc/self/cgroup').read_text().splitlines()
relative = next((line.split('::', 1)[1] for line in membership if '::' in line), None)
mount_line = next(
    (line for line in Path('/proc/self/mountinfo').read_text().splitlines()
     if ' - cgroup2 ' in line),
    None,
)
if relative is None or mount_line is None:
    print('resolved_job_cgroup=unavailable')
    raise SystemExit(0)

mount_fields = mount_line.partition(' - ')[0].split()
mount_root = mount_fields[3].replace('\\040', ' ')
mountpoint = mount_fields[4].replace('\\040', ' ')
if relative == mount_root:
    suffix = ''
elif relative.startswith(mount_root.rstrip('/') + '/'):
    suffix = relative[len(mount_root.rstrip('/')):].lstrip('/')
else:
    print('resolved_job_cgroup=unavailable')
    raise SystemExit(0)

directory = Path(mountpoint) / suffix
print(f'resolved_job_cgroup={directory}')
memory_limits = []
swap_limits = []
metric_names = (
    'memory.current',
    'memory.peak',
    'memory.max',
    'memory.events',
    'memory.swap.current',
    'memory.swap.max',
    'memory.swap.events',
    'cpu.stat',
)
ancestors = (directory, *directory.parents)
for index, ancestor in enumerate(ancestors):
    if not ancestor.is_relative_to(Path(mountpoint)):
        break
    print(f'cgroup_ancestor[{index}]={ancestor}')
    for name in metric_names:
        path = ancestor / name
        try:
            value = path.read_text().rstrip()
        except OSError as error:
            value = f'unavailable: {error}'
        print(f'cgroup_metric[{index}]={name}')
        print(value)
        if name in ('memory.max', 'memory.swap.max') and value.isdigit():
            (memory_limits if name == 'memory.max' else swap_limits).append(int(value))

mem_total_kib = None
for line in Path('/proc/meminfo').read_text().splitlines():
    if line.startswith('MemTotal:'):
        mem_total_kib = int(line.split()[1])
        break
memory_bounds = list(memory_limits)
if mem_total_kib is not None:
    memory_bounds.append(mem_total_kib * 1024)
print(f'runner_mem_total_bytes={mem_total_kib * 1024 if mem_total_kib is not None else "unavailable"}')
print(f'effective_memory_limit_bytes={min(memory_bounds) if memory_bounds else "unavailable"}')
print(f'effective_swap_limit_bytes={min(swap_limits) if swap_limits else "unavailable"}')
