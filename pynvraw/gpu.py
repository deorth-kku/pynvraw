import ctypes
import typing

from .nvapi_api import NvAPI, NvPhysicalGpu, NV_GPU_THERMAL_SETTINGS, NVAPI_THERMAL_TARGET_ALL, NVAPI_THERMAL_TARGET_GPU, \
        NvAPI_ShortString, NV_GPU_CLOCK_FREQUENCIES_CURRENT_FREQ, NV_GPU_CLOCK_FREQUENCIES_BASE_CLOCK, NV_GPU_CLOCK_FREQUENCIES_BOOST_CLOCK, \
        NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS, NVAPI_GPU_PUBLIC_CLOCK_MEMORY, NVAPI_GPU_PUBLIC_CLOCK_PROCESSOR, NVAPI_GPU_PUBLIC_CLOCK_VIDEO, \
        NV_GPU_POWER_STATUS
from .status import NvError

class Delta(typing.NamedTuple):
    current: float
    min: float
    max: float

class Clocks(typing.NamedTuple):
    core: float
    memory: float
    processor: float
    video: float

class ClockDelta(typing.NamedTuple):
    core: Delta
    memory: Delta
    processor: Delta
    video: Delta

domains = {NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS: 'core', NVAPI_GPU_PUBLIC_CLOCK_MEMORY: 'memory',
           NVAPI_GPU_PUBLIC_CLOCK_PROCESSOR: 'processor', NVAPI_GPU_PUBLIC_CLOCK_VIDEO: 'video'}

class Gpu:
    '''Wrapper over low-level NvPhysicalGpu structure.'''
    def __init__(self, handle: NvPhysicalGpu, api: NvAPI):
        self.handle = handle
        self.api = api
        self.__name = None

    @property
    def core_temp(self) -> int:
        '''Reads gpu core thermal sensor in Celsius.'''
        thermal = NV_GPU_THERMAL_SETTINGS()
        self.api.NvAPI_GPU_GetThermalSettings(self.handle, NVAPI_THERMAL_TARGET_ALL, ctypes.pointer(thermal))
        for sensor in thermal.sensor:
            if sensor.target == NVAPI_THERMAL_TARGET_GPU:
                return sensor.currentTemp
        raise ValueError('Cannot find gpu temperature sensor')
    
    @property
    def hotspot_temp(self) -> typing.Union[int, None]:
        '''Reads hotspot thermal sensor in Celsius if present, None otherwise.'''
        try:
            hotspot, _ = self.api.get_temps_ex(self.handle)
            return hotspot
        except NvError as ex:
            if ex.status == 'NVAPI_NOT_SUPPORTED':
                return None
            raise

    @property
    def vram_temp(self) -> typing.Union[int, None]:
        '''Reads memory thermal sensor in Celsius if present, None otherwise.'''
        try:
            _, vram = self.api.get_temps_ex(self.handle)
            return vram
        except NvError as ex:
            if ex.status == 'NVAPI_NOT_SUPPORTED':
                return None
            raise

    @property
    def name(self) -> str:
        '''Reads GPU device name.'''
        if self.__name is None:
            name = NvAPI_ShortString()
            self.api.NvAPI_GPU_GetFullName(self.handle, ctypes.pointer(name))
            self.__name = name.value.decode('utf8')
        return self.__name

    @property
    def fan(self) -> int:
        '''Reads cooler duty cycle in % (if multiple coolers present reports lowest duty).'''
        try:
            settings = self.api.get_cooler_settings(self.handle)
        except NvError as ex:
            if ex.status == 'NVAPI_NOT_SUPPORTED':
                return None
            raise
        levels = [cooler.current_level for cooler in settings.coolers[:settings.count]]
        return min(levels)

    @fan.setter
    def fan(self, value):
        '''Set _all_ coolers duty cycles (in %).'''
        self.api.set_cooler_duty(self.handle, 0, value)

    @staticmethod
    def __cast_domain_freq(freqs, clock_id):
        if freqs.domain[clock_id].bIsPresent != 0:
            return freqs.domain[clock_id].frequency / 1000
        return None

    def get_freqs(self, clock_type_str: str) -> Clocks:
        '''Reads clocks for given clock type: "current", "base" or "boost".'''
        clock_type = {
                'current': NV_GPU_CLOCK_FREQUENCIES_CURRENT_FREQ,
                'base': NV_GPU_CLOCK_FREQUENCIES_BASE_CLOCK,
                'boost': NV_GPU_CLOCK_FREQUENCIES_BOOST_CLOCK}[clock_type_str.lower()]
        value = self.api.get_freqs(self.handle, clock_type)
        known = {NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS, NVAPI_GPU_PUBLIC_CLOCK_MEMORY, NVAPI_GPU_PUBLIC_CLOCK_PROCESSOR, NVAPI_GPU_PUBLIC_CLOCK_VIDEO}
        for i in range(len(value.domain)):
            if i in known:
                continue
            if value.domain[i].bIsPresent != 0:
                print(f'Unknown domain #{i} present, freq={value.domain[i].frequency / 1000}')
        return Clocks(core=self.__cast_domain_freq(value, NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS), 
                      memory=self.__cast_domain_freq(value, NVAPI_GPU_PUBLIC_CLOCK_MEMORY),
                      processor=self.__cast_domain_freq(value, NVAPI_GPU_PUBLIC_CLOCK_PROCESSOR), 
                      video=self.__cast_domain_freq(value, NVAPI_GPU_PUBLIC_CLOCK_VIDEO))

    def get_overclock(self) -> ClockDelta:
        '''Reads current overclocking settings (current delta and minimum-maximum pair for each clock).'''
        states = self.api.get_pstates(self.handle)
        assert states.numPstates > 0 and states.pstates[0].bIsEditable
        p0 = states.pstates[0]
        result = ClockDelta(None, None, None, None)

        for clockIdx in range(states.numClocks):
            clock = p0.clocks[clockIdx]
            if clock.domainId not in domains:
                continue
            delta = Delta(current=clock.freqDelta_kHz.value / 1000, min=clock.freqDelta_kHz.valueMin / 1000, max=clock.freqDelta_kHz.valueMax / 1000)
            result = result._replace(**{domains[clock.domainId]: delta})
        return result            

    def set_overclock(self, delta: Clocks):
        '''Overclocks the GPU by applying deltas to given clocks. Specify None to a clock to not touch it.'''
        states = self.api.get_pstates(self.handle)
        assert states.numPstates > 0 and states.pstates[0].bIsEditable
        states.numPstates = 1
        p0 = states.pstates[0]
        clockIdx = 0
        for domainId, domainName in domains.items():
            value = getattr(delta, domainName, None)
            if value is None:
                continue
            clock = p0.clocks[clockIdx]
            if value * 1000 < clock.freqDelta_kHz.valueMin or value * 1000 > clock.freqDelta_kHz.valueMax:
                raise ValueError(f'Value for {domainName} ({value}) is out of range ({clock.freqDelta_kHz.valueMin/1000}-{clock.freqDelta_kHz.valueMax/1000})')
            clock.freqDelta_kHz.value = int(value * 1000)
            clock.domainId = domainId
            clockIdx += 1
        states.numClocks = clockIdx
        self.api.NvAPI_GPU_SetPstates20(self.handle, ctypes.pointer(states))

    @property
    def power_limit(self) -> float:
        '''Reads current power limit in %.'''
        status = self.api.get_power_status(self.handle)
        if status.count == 0:
            return None
        return max(e.power for e in status.entries[:status.count]) / 1000

    @power_limit.setter
    def power_limit(self, value: float):
        '''Sets current power limit in %.'''
        status = NV_GPU_POWER_STATUS()
        status.count = 1
        status.entries[0].power = int(value * 1000)
        self.api.NvAPI_GPU_ClientPowerPoliciesSetStatus(self.handle, ctypes.pointer(status))

    @property
    def power(self) -> float:
        '''Reads current power consumption in %.'''
        status = self.api.get_topology_status(self.handle)
        if status.count == 0:
            return None
        return status.unknown[2] / 1000
