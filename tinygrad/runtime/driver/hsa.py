import ctypes, collections
import tinygrad.runtime.autogen.hsa as hsa
from tinygrad.helpers import init_c_var

def check(status):
  if status != 0:
    hsa.hsa_status_string(status, ctypes.byref(status_str := ctypes.POINTER(ctypes.c_char)()))
    raise RuntimeError(f"HSA Error {status}: {ctypes.string_at(status_str).decode()}")

## AQL packets

# Precalulated AQL info
AQL_PACKET_SIZE = ctypes.sizeof(hsa.hsa_kernel_dispatch_packet_t)
EMPTY_SIGNAL = hsa.hsa_signal_t()

DISPATCH_KERNEL_SETUP = 3 << hsa.HSA_KERNEL_DISPATCH_PACKET_SETUP_DIMENSIONS
DISPATCH_KERNEL_HEADER  = 1 << hsa.HSA_PACKET_HEADER_BARRIER
DISPATCH_KERNEL_HEADER |= hsa.HSA_FENCE_SCOPE_SYSTEM << hsa.HSA_PACKET_HEADER_SCACQUIRE_FENCE_SCOPE
DISPATCH_KERNEL_HEADER |= hsa.HSA_FENCE_SCOPE_SYSTEM << hsa.HSA_PACKET_HEADER_SCRELEASE_FENCE_SCOPE
DISPATCH_KERNEL_HEADER |= hsa.HSA_PACKET_TYPE_KERNEL_DISPATCH << hsa.HSA_PACKET_HEADER_TYPE

BARRIER_HEADER  = 1 << hsa.HSA_PACKET_HEADER_BARRIER
BARRIER_HEADER |= hsa.HSA_FENCE_SCOPE_SYSTEM << hsa.HSA_PACKET_HEADER_SCACQUIRE_FENCE_SCOPE
BARRIER_HEADER |= hsa.HSA_FENCE_SCOPE_SYSTEM << hsa.HSA_PACKET_HEADER_SCRELEASE_FENCE_SCOPE
BARRIER_HEADER |= hsa.HSA_PACKET_TYPE_BARRIER_AND << hsa.HSA_PACKET_HEADER_TYPE

class amd_aql_pm4_packet_t(ctypes.Structure):
    _fields_ = [('header', ctypes.c_uint16), ('format', ctypes.c_uint16), ('pm4_cmds', ctypes.c_uint32*13), ('completion_signal', hsa.hsa_signal_t)]

class AQLQueue:
  def __init__(self, device, sz=-1):
    self.device = device

    check(hsa.hsa_agent_get_info(self.device.agent, hsa.HSA_AGENT_INFO_QUEUE_MAX_SIZE, ctypes.byref(max_queue_size := ctypes.c_uint32())))
    queue_size = min(max_queue_size.value, sz) if sz != -1 else max_queue_size.value

    null_func = ctypes.CFUNCTYPE(None, hsa.hsa_status_t, ctypes.POINTER(hsa.struct_hsa_queue_s), ctypes.c_void_p)()
    self.hw_queue = init_c_var(ctypes.POINTER(hsa.hsa_queue_t)(), lambda x: check(
      hsa.hsa_queue_create(self.device.agent, queue_size, hsa.HSA_QUEUE_TYPE_SINGLE, null_func, None, (1<<32)-1, (1<<32)-1, ctypes.byref(x))))

    self.next_doorbell_index = 0
    self.queue_size = self.hw_queue.contents.size
    self.write_addr = self.hw_queue.contents.base_address
    self.write_addr_end = self.hw_queue.contents.base_address + (AQL_PACKET_SIZE * self.queue_size) - 1
    self.available_packet_slots = self.queue_size

    check(hsa.hsa_amd_queue_set_priority(self.hw_queue, hsa.HSA_AMD_QUEUE_PRIORITY_HIGH))
    check(hsa.hsa_amd_profiling_set_profiler_enabled(self.hw_queue, 1))

  def __del__(self):
    if hasattr(self, 'hw_queue'): check(hsa.hsa_queue_destroy(self.hw_queue))

  def submit_kernel(self, prg, global_size, local_size, kernargs, need_signal=False):
    if self.available_packet_slots == 0: self._wait_queue()
    signal = self._alloc_signal(reusable=True) if need_signal else EMPTY_SIGNAL

    packet = hsa.hsa_kernel_dispatch_packet_t.from_address(self.write_addr)
    packet.workgroup_size_x = local_size[0]
    packet.workgroup_size_y = local_size[1]
    packet.workgroup_size_z = local_size[2]
    packet.reserved0 = 0
    packet.grid_size_x = global_size[0] * local_size[0]
    packet.grid_size_y = global_size[1] * local_size[1]
    packet.grid_size_z = global_size[2] * local_size[2]
    packet.private_segment_size = prg.private_segment_size
    packet.group_segment_size = prg.group_segment_size
    packet.kernel_object = prg.handle
    packet.kernarg_address = kernargs
    packet.reserved2 = 0
    packet.completion_signal = signal
    packet.setup = DISPATCH_KERNEL_SETUP
    packet.header = DISPATCH_KERNEL_HEADER
    self._submit_packet()

    return signal

  def submit_barrier(self, wait_signals=None, need_signal=False, completion_signal=None):
    assert wait_signals is None or len(wait_signals) <= 5
    if self.available_packet_slots == 0: self._wait_queue()
    signal = (completion_signal or self._alloc_signal(reusable=True)) if need_signal else EMPTY_SIGNAL

    packet = hsa.hsa_barrier_and_packet_t.from_address(self.write_addr)
    packet.reserved0 = 0
    packet.reserved1 = 0
    for i in range(5):
      packet.dep_signal[i] = wait_signals[i] if wait_signals and len(wait_signals) > i else EMPTY_SIGNAL
    packet.reserved2 = 0
    packet.completion_signal = signal
    packet.header = BARRIER_HEADER
    self._submit_packet()

    return signal

  def submit_pm4_ib(self, ib_address, ib_size, need_signal=False, completion_signal=None):
    if self.available_packet_slots == 0: self._wait_queue()
    signal = (completion_signal or self._alloc_signal(reusable=True)) if need_signal else EMPTY_SIGNAL

    indirect_exec_cmd = pm4_build_indirect_command(ib_address, ib_size)

    ctypes.memset(self.write_addr, 0, 64)
    packet = amd_aql_pm4_packet_t.from_address(self.write_addr)
    packet.format = 0x1 # AMD_AQL_FORMAT_PM4_IB
    for i, value in enumerate(indirect_exec_cmd): packet.pm4_cmds[i] = value
    packet.pm4_cmds[len(indirect_exec_cmd)] = 14 - len(indirect_exec_cmd) # remain dwords count
    packet.completion_signal = signal
    packet.header = hsa.HSA_PACKET_TYPE_VENDOR_SPECIFIC << hsa.HSA_PACKET_HEADER_TYPE
    self._submit_packet()

    return signal

  def blit_packets(self, packet_addr, packet_cnt):
    if self.available_packet_slots < packet_cnt: self._wait_queue(packet_cnt)

    tail_blit_packets = min(((self.write_addr_end + 1) - self.write_addr) // AQL_PACKET_SIZE, packet_cnt)
    rem_packet_cnt = packet_cnt - tail_blit_packets
    ctypes.memmove(self.write_addr, packet_addr, AQL_PACKET_SIZE * tail_blit_packets)
    self.write_addr += AQL_PACKET_SIZE * tail_blit_packets
    if self.write_addr > self.write_addr_end: self.write_addr = self.hw_queue.contents.base_address
    if tail_blit_packets > 0:
      ctypes.memmove(self.write_addr, packet_addr + AQL_PACKET_SIZE * tail_blit_packets, AQL_PACKET_SIZE * rem_packet_cnt)
      self.write_addr += AQL_PACKET_SIZE * rem_packet_cnt

    self.next_doorbell_index += packet_cnt
    hsa.hsa_queue_store_write_index_screlease(self.hw_queue, self.next_doorbell_index + 1)
    hsa.hsa_signal_store_screlease(self.hw_queue.contents.doorbell_signal, self.next_doorbell_index)

  def wait(self):
    signal = self.submit_barrier(need_signal=True)
    hsa.hsa_signal_wait_scacquire(signal, hsa.HSA_SIGNAL_CONDITION_LT, 1, (1 << 64) - 1, hsa.HSA_WAIT_STATE_ACTIVE)
    self.available_packet_slots = self.queue_size

  def _wait_queue(self, need_packets=1):
    while self.available_packet_slots < need_packets:
      rindex = hsa.hsa_queue_load_read_index_relaxed(self.hw_queue)
      self.available_packet_slots = self.queue_size - (self.next_doorbell_index - rindex)

  def _submit_packet(self):
    hsa.hsa_queue_store_write_index_relaxed(self.hw_queue, self.next_doorbell_index + 1)
    hsa.hsa_signal_store_screlease(self.hw_queue.contents.doorbell_signal, self.next_doorbell_index)

    self.write_addr += AQL_PACKET_SIZE
    if self.write_addr > self.write_addr_end: self.write_addr = self.hw_queue.contents.base_address
    self.next_doorbell_index += 1
    self.available_packet_slots -= 1

  def _alloc_signal(self, reusable=False): return self.device.alloc_signal(reusable=reusable)

## PM4 packets

PM4_HDR_IT_OPCODE_INDIRECT_BUFFER = 0x3f
PM4_INDIRECT_BUFFER_VALID = 1 << 23

PM4_HDR_IT_OPCODE_ACQUIRE_MEM = 0x58
PM4_ACQUIRE_MEM_GCR_CNTL_GLI_INV = 1 << 0
PM4_ACQUIRE_MEM_GCR_CNTL_GLK_INV = 1 << 7
PM4_ACQUIRE_MEM_GCR_CNTL_GLV_INV = 1 << 8
PM4_ACQUIRE_MEM_GCR_CNTL_GL1_INV = 1 << 9
PM4_ACQUIRE_MEM_GCR_CNTL_GL2_INV = 1 << 14

def pm4_header(op, cmd_size): return 3 << 30 | ((cmd_size - 2) & 0x3FFF) << 16 | (((op) & 0xFF) << 8)
def pm4_build_indirect_command(ib_addr, ib_sz):
  return [pm4_header(PM4_HDR_IT_OPCODE_INDIRECT_BUFFER, 4), ib_addr & 0xffffffff, (ib_addr>>32) & 0xffffffff, (ib_sz//4) | PM4_INDIRECT_BUFFER_VALID]
def pm4_build_cache_inv_command(addr=0, sz=0xffffffffff):
  return [pm4_header(PM4_HDR_IT_OPCODE_ACQUIRE_MEM, 8), 0,
          sz & 0xffffffff, (sz >> 32) & 0xffffffff, addr & 0xffffffff, (addr >> 32) & 0xffffffff, 0,
          PM4_ACQUIRE_MEM_GCR_CNTL_GLI_INV | PM4_ACQUIRE_MEM_GCR_CNTL_GLK_INV | PM4_ACQUIRE_MEM_GCR_CNTL_GLV_INV | \
          PM4_ACQUIRE_MEM_GCR_CNTL_GL1_INV | PM4_ACQUIRE_MEM_GCR_CNTL_GL2_INV ]

## Agents

def scan_agents():
  agents = collections.defaultdict(list)

  @ctypes.CFUNCTYPE(hsa.hsa_status_t, hsa.hsa_agent_t, ctypes.c_void_p)
  def __scan_agents(agent, data):
    status = hsa.hsa_agent_get_info(agent, hsa.HSA_AGENT_INFO_DEVICE, ctypes.byref(device_type := hsa.hsa_device_type_t()))
    if status == 0: agents[device_type.value].append(agent)
    return hsa.HSA_STATUS_SUCCESS

  hsa.hsa_iterate_agents(__scan_agents, None)
  return agents

def find_memory_pool(agent, segtyp=-1, location=-1):
  @ctypes.CFUNCTYPE(hsa.hsa_status_t, hsa.hsa_amd_memory_pool_t, ctypes.c_void_p)
  def __filter_amd_memory_pools(mem_pool, data):
    check(hsa.hsa_amd_memory_pool_get_info(mem_pool, hsa.HSA_AMD_MEMORY_POOL_INFO_SEGMENT, ctypes.byref(segment := hsa.hsa_amd_segment_t())))
    if segtyp >= 0 and segment.value != segtyp: return hsa.HSA_STATUS_SUCCESS

    check(hsa.hsa_amd_memory_pool_get_info(mem_pool, hsa.HSA_AMD_MEMORY_POOL_INFO_LOCATION, ctypes.byref(loc:=hsa.hsa_amd_memory_pool_location_t())))
    if location >= 0 and loc.value != location: return hsa.HSA_STATUS_SUCCESS

    check(hsa.hsa_amd_memory_pool_get_info(mem_pool, hsa.HSA_AMD_MEMORY_POOL_INFO_SIZE, ctypes.byref(sz := ctypes.c_size_t())))
    if sz.value == 0: return hsa.HSA_STATUS_SUCCESS

    ret = ctypes.cast(data, ctypes.POINTER(hsa.hsa_amd_memory_pool_t))
    ret[0] = mem_pool
    return hsa.HSA_STATUS_INFO_BREAK

  hsa.hsa_amd_agent_iterate_memory_pools(agent, __filter_amd_memory_pools, ctypes.byref(region := hsa.hsa_amd_memory_pool_t()))
  return region
