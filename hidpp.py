import asyncio
import logging.config

from enum import Enum

import hid
from dataclasses import dataclass, field
from typing import Optional, Dict, Union

from threading import Event
from threading import Thread

from consts import HIDPPConstants
from messages import HIDPP10Message, HIDPP20Message

logging.config.fileConfig('logging.conf')
LOG = logging.getLogger('HID++')

# TODO: improve using full protocol
# https://drive.google.com/file/d/0BxbRzx7vEV7eU3VfMnRuRXktZ3M/view?resourcekey=0-06JzoS5yy_4Asod95f4Ecw


class HIDPP10Messages(Enum):
    """
    Fixed HIDPP Messages used for device enumeration
    """
    GetDeviceCount = HIDPP10Message.create(is_long=False, device_index=0xFF, sub_id=0x81,
                                           parameters=[0x02, 0x00, 0x00, 0x00])
    AnnouceArrival = HIDPP10Message.create(is_long=False, device_index=0xFF, sub_id=0x80,
                                           parameters=[0x02, 0x02, 0x00, 0x00])


@dataclass
class BatteryStateUpdate:
    """
    A battery state
    """
    percent: int
    status: str


class HIDPPDeviceChannel:
    """
    A communication channel to a HID++ Device.
    The device processes requests asynchronously and outputs responses into a queue.
    Each HID++ device has two os devices which can serve as destinations for requests. Os devices can send a response
    given to a device through the other, Thus both share a response queue.
    """

    def __init__(self, dev: hid.Device, message_size: int, parent_pair: 'HIDPPDevicePair'):
        # The os device
        self.dev = dev
        # Each device is responsible for responding to fixed-sized requests, requests can be either short or long
        self.message_size = message_size
        # The parent pair object has fields shared by both devices, such as the response queue
        self.parent_pair = parent_pair
        # An event for stopping processing messages
        self._stop_event = Event()
        # A thread which processes device requests
        self._thread = Thread(name=f'{hex(parent_pair.product_id)}_{self.message_size}',
                              target=self._read_responses, args=(asyncio.get_event_loop(),), daemon=True)

    def _read_responses(self, loop: asyncio.BaseEventLoop) -> None:
        """
        Consume device responses and write them into a queue

        :note: hidapi hasn't implemented an async read function for hid devices,
               see https://github.com/libusb/hidapi/issues/202
        :return: None
        """
        self.dev.nonblocking = 0
        while not self._stop_event.is_set():
            try:
                data = self.dev.read(self.message_size, timeout=1)

                if not data:
                    continue

                if data[2] == 0x41 and (data[4] & 0x40 == 0):
                    loop.call_soon_threadsafe(self.parent_pair.initialization_queue.put_nowait, data)
                    continue

                loop.call_soon_threadsafe(self.parent_pair.response_queue.put_nowait, data)
            except:
                LOG.exception(f"Unexpected exception")
                return

    def start_reader_thread(self) -> None:
        """
        Start a thread which reads responses from the device asynchronously into a queue
        """
        self._thread.start()

    def stop_reader_thread(self, timeout_seconds: Optional[float] = None) -> None:
        """
        Signal the async read thread to stop and wait for it to stop
        :param timeout_seconds time to wait for the thread to stop in seconds
        """
        self._stop_event.set()
        self._thread.join(timeout=timeout_seconds)

    async def send_request(self, message: Union[HIDPP20Message, HIDPP10Message], timeout_seconds=5) \
            -> Optional[Union[HIDPP20Message, HIDPP10Message]]:
        """
        Sends a HID++ request to the device and waits for a response
        :param message: A request message
        :param timeout_seconds: Timeout for retrieving the response
        :return: Device response for the request or TimeoutError
        """
        LOG.debug(f'HID++ write: %s, %s', message, self.parent_pair.product_id)
        self.dev.write(message.data)

        response = await asyncio.wait_for(self._get_response(message), timeout=timeout_seconds)
        LOG.debug(f"HID++ request %s, response %s, product: %s", message, response,
                  self.parent_pair.product_id)

        return response

    async def _get_response(self, request: Union[HIDPP20Message, HIDPP10Message]):
        """
        Returns a response to a HID++ request
        :param request:
        :return:
        """
        is_hidpp10 = isinstance(request, HIDPP10Message)
        message_queue = self.parent_pair.response_queue

        while True:
            message_type_cls = request.__class__
            response = message_type_cls(await message_queue.get())
            message_queue.task_done()

            if is_hidpp10 and (response.feature_index == HIDPPConstants.HIDPP10InvalidSubID.value or
                               response.feature_index == request.feature_index):
                LOG.debug(f"Response: {response}")
                return response

            if not is_hidpp10 and response.feature_index == HIDPPConstants.HIDPP10InvalidSubID.value:
                LOG.warning(f"Message %s got invalid HIDPP10 response %s", request, response)
                continue

            if not is_hidpp10 and response.software_id == HIDPPConstants.SoftwareID.value and \
                    response.feature_index == request.feature_index:
                LOG.debug(f"Response {response}")
                return response


class HIDPPDevicePair:
    """
    Each HID++ device has two os devices to communicate with, one for short messages and one for long messages.
    This class contains the structures needed for establishing and maintaining communication using both devices.
    """

    def __init__(self, product_id: str):
        # HID Product identifier
        self.product_id = product_id
        # Contains device responses to messages sent by an application, can contain both long and short messages,
        # as requests of a certain length can result in responses of multiple lengths
        self.response_queue = asyncio.Queue(maxsize=1 << 4)
        # Contains device responses to messages sent as part of initialization
        self.initialization_queue = asyncio.Queue(maxsize=1 << 4)

        all_devices = [dev_dict for dev_dict in hid.enumerate(HIDPPConstants.VendorId.value, product_id)
                       if (dev_dict['usage_page'] & 0xff00) == 0xff00]
        short_devices = [dev_dict for dev_dict in all_devices if
                         dev_dict['usage'] == HIDPPConstants.HIDShortDevUsage.value and dev_dict[
                             'product_id'] == product_id]
        long_devices = [dev_dict for dev_dict in all_devices if
                        dev_dict['usage'] == HIDPPConstants.HIDLongDevUsage.value and dev_dict[
                            'product_id'] == product_id]

        # Didn't find complete cross-platform solution for this issue,
        # can use interface_number to discriminate usb-connected devices
        assert len(short_devices) == 1 and len(long_devices) == 1, "Multiple identical products are not supported"
        assert len(set(dev['path'] for dev in short_devices + long_devices)) == 2, \
            "Expected different short and long devices"
        # Short device
        self.short_dev = HIDPPDeviceChannel(hid.Device(path=short_devices[0]['path']),
                                            HIDPPConstants.ShortMessageLength.value,
                                            self)
        # Long device
        self.long_dev = HIDPPDeviceChannel(hid.Device(path=long_devices[0]['path']),
                                           HIDPPConstants.LongMessageLength.value,
                                           self)

    def __contains__(self, dev: hid.Device):
        """
        :param dev: An HID device
        :return: True if dev is either the short device or the long device in this pair
        """
        return any(dev == x for x in self)

    def __iter__(self):
        yield self.short_dev
        yield self.long_dev


@dataclass
class HIDPP20Device:
    """
    An HID++ 2.0 Compatible Device
    """
    # The HID++ device pair
    device_pair: HIDPPDevicePair
    # An identifier for a specific device, multiple physical devices can be connected for a single
    # hardware device, such as a unifying receiver
    device_index: int
    # HID++ 2.0 devices implement categorized functions named features
    # Each feature has an identifier shared by all devices that implement this feature,
    # such as the 0x1004 feature for retrieving battery state and a feature index which is used
    # to indentify a feature in a specific device
    features: Dict = field(default_factory=dict)
    # Textual representation of the device
    name: Optional[str] = None
    # The following are device identifiers, some of which might not be available for specific hardware
    unit_id: Optional[str] = None
    model_id: Optional[str] = None
    serial: Optional[str] = None

    @classmethod
    async def create(cls, device_pair: HIDPPDevicePair, device_index: int):
        """
        Constructs and initializes an HID++ 2.0 device
        :param device_pair:
        :param device_index:
        :return: An initialized device object
        """
        hidppdevice = cls(device_pair, device_index)
        await hidppdevice._init_device()
        return hidppdevice

    def __str__(self):
        return f'{self.name} - {self.unit_id} - {self.model_id} - {self.serial} - {self.features.keys()}'

    def __hash__(self):
        return hash(str(self))

    async def _send_device_req(self, feature_index, function_id, parameters=None, is_long=False) -> \
            Optional[HIDPP20Message]:
        """
        Send a HID++ 2.0 request to the device
        :param feature_index: See features documentation
        :param function_id: Each feature implements multiple functions which can
                            read/write/both feature-related state
        :param parameters: Parameters for the invoked function
        :param is_long: Should the request be sent to the long or short device
        :return: A device response
        """
        dev = self.device_pair.long_dev if is_long else self.device_pair.short_dev
        return await dev.send_request(HIDPP20Message.create(
            device_index=self.device_index, feature_index=feature_index, function_id=function_id,
            parameters=parameters, is_long=is_long))

    async def _verify_device_communication(self, pings_to_send: int = 1, success_threshold: int = 1):
        """
        Send pings to device to ensure the communication channel is valid
        :param pings_to_send: Amount of ping requests to send
        :param success_threshold: Expected number of valid responses
        """
        payload = 0x17
        success_count = 0
        for i in range(pings_to_send):
            payload += i
            ret = await self._send_device_req(feature_index=0, function_id=1, parameters=[0x00, 0x00, payload])
            if not ret:
                continue
            success_count += int(ret.feature_index == 0x00 and ret.software_id == HIDPPConstants.SoftwareID.value
                                 and ret.parameter(2) == payload)
            if success_count == success_threshold:
                break

        if success_count != success_threshold:
            raise RuntimeError(f"Could not initialize HID++2.0 device, device index: {self.device_index}")

    async def _init_device(self, iterations: int = 1, threshold: int = 1,
                           shortcircuit_feature_ids=((0x1001, 0x3, 0x5), (0x1004, 0x3, 0x5))):
        await self._verify_device_communication(iterations, threshold)
        await self._enumerate_device_features(shortcircuit_feature_ids)

        device_name_feature_id = 0x5
        assert device_name_feature_id in self.features, "Cannot extract device name"

        ret = await self._send_device_req(feature_index=self.features[device_name_feature_id], function_id=0)
        name_length = ret.parameter(0)
        name = ""
        while len(name) < name_length:
            ret = await self._send_device_req(feature_index=self.features[device_name_feature_id], function_id=1,
                                              parameters=[len(name), 0x00, 0x00])
            name += bytes(ret.parameters).decode('utf-8')
        name = name.rstrip("\0")
        self.name = name

        ret = await self._send_device_req(feature_index=self.features[device_name_feature_id], function_id=2)
        type = ret.parameter(0)
        LOG.info(f'Device type is: %s', type)

        await self._init_device_identifiers()

    async def _enumerate_device_features(self, shortcircuit_feature_ids):
        LOG.info(f"Initializing features for device {self.device_index}")
        root_feature_id = 0x0001
        ret = await self._send_device_req(feature_index=0, function_id=0, parameters=[0x00, 0x01, 0x00])
        self.features[root_feature_id] = ret.parameter(0)
        root_feature_index = self.features[root_feature_id]

        LOG.info(f"Getting feature count")
        ret = await self._send_device_req(feature_index=root_feature_index, function_id=0)
        feature_count = ret.parameter(0)
        LOG.info(f"Feature count is: %s", feature_count)

        for feature_index in range(feature_count + 1):
            ret = await self._send_device_req(feature_index=root_feature_index, function_id=1,
                                              parameters=[feature_index, 0x00, 0x00])
            feature_id = (ret.parameter(0) << 8) | ret.parameter(1)
            self.features[feature_id] = feature_index
            LOG.debug(f"Found feature %s in index %s", hex(feature_id), feature_index)

            if shortcircuit_feature_ids and \
                    any(all(feature in self.features for feature in feature_list)
                        for feature_list in shortcircuit_feature_ids):
                break

    async def _init_device_identifiers(self):
        device_identity_feature_id = 0x3
        ret = await self._send_device_req(feature_index=self.features[device_identity_feature_id], function_id=0)
        self.unit_id = ret.parameters[1:5].hex()
        self.model_id = ret.parameters[7:12].hex()
        LOG.debug(f'data: %s, unit_id: %s model_id: %s', ret.data.hex(" "), self.unit_id, self.model_id)

        serial_supported = ret.parameter(14) & 0x1 == 0x1
        if serial_supported:
            ret = await self._send_device_req(self.features[device_identity_feature_id], function_id=2)
            self.serial = ret.parameters[:11].hex()

    def has_feature(self, feature_code: int):
        return feature_code in self.features

    async def _get_battery_1001(self):
        assert self.has_feature(0x1001)
        LOG.info(f"Getting 0x1001 from %s", self.name)
        ret = await self._send_device_req(feature_index=self.features[0x1001], function_id=0)

        status_bits_to_description = {
            0: 'charging',
            1: 'full',
            2: 'not charging',
        }
        lut = [3537, 3579, 3612, 3633, 3646, 3654, 3658, 3662, 3666, 3671, 3677, 3683, 3688, 3693, 3697, 3702, 3706,
               3710, 3714, 3717, 3720, 3724, 3726, 3730, 3734, 3737, 3741, 3744, 3748, 3751, 3754, 3757, 3759, 3762,
               3764, 3767, 3770, 3772, 3775, 3778, 3781, 3784, 3787, 3790, 3793, 3797, 3800, 3804, 3808, 3811, 3815,
               3819, 3824, 3828, 3833, 3837, 3842, 3848, 3853, 3859, 3865, 3870, 3877, 3883, 3890, 3896, 3902, 3909,
               3916, 3922, 3929, 3935, 3942, 3949, 3955, 3961, 3969, 3976, 3983, 3989, 3997, 4003, 4011, 4019, 4027,
               4035, 4043, 4051, 4059, 4067, 4075, 4086, 4094, 4103, 4113, 4122, 4133, 4143, 4156, 4186]

        voltage = (ret.parameter(0) << 8) | ret.parameter(1)
        percent = 0
        if voltage > max(lut):
            percent = 100
        elif voltage < min(lut):
            percent = 0
        else:
            percent = next(filter(lambda x: x[1] > voltage, enumerate(lut)))[0]
        flags = ret.parameter(2)
        if flags & 0x80 > 0:
            status_bits = flags & 0x07
            status = status_bits_to_description[status_bits] if status_bits in status_bits_to_description else 'Unknown'
        else:
            status = 'discharging'

        return BatteryStateUpdate(percent=percent, status=status)

    async def _get_battery_1004(self):
        assert self.has_feature(0x1004)
        LOG.info(f"Getting 0x1004 from {self.name}")
        ret = await self._send_device_req(feature_index=self.features[0x1004], function_id=1)
        assert len(ret) == 20

        statuses = {
            0: 'discharging',
            1: 'charging',
            2: 'charging',
            3: 'full',
        }

        percent = ret.parameter(0)
        status = statuses[ret.parameter(2)] if ret.parameter(2) in statuses else 'unknown'

        return BatteryStateUpdate(percent=percent, status=status)

    async def get_battery(self):
        feature_to_func = {0x1001: self._get_battery_1001, 0x1004: self._get_battery_1004}
        for feature_id, getter in feature_to_func.items():
            if self.has_feature(feature_id):
                return await getter()

# https://github.com/cvuchener/hidpp


def get_device_pairs(vendor_id) -> list[HIDPPDevicePair]:
    all_devices = [dev_dict for dev_dict in hid.enumerate(vendor_id) if (dev_dict['usage_page'] & 0xff00) == 0xff00]
    product_ids = list(set(dev_dict["product_id"] for dev_dict in all_devices))
    LOG.debug("Found product ids: %s", product_ids)
    return [HIDPPDevicePair(product_id) for product_id in product_ids]


async def register_devices(hid_device_pair: HIDPPDevicePair):
    hidpp20_devices = []

    device_count = await get_device_count(hid_device_pair.short_dev)
    if not device_count:
        LOG.info(f"No devices for %s", hid_device_pair.product_id)
        return

    await hid_device_pair.short_dev.send_request(HIDPP10Messages.AnnouceArrival.value)

    while device_count > 0:
        data = await hid_device_pair.initialization_queue.get()
        LOG.debug(f"Got arrival response {data.hex(' ')}")
        hid_device_pair.initialization_queue.task_done()

        device_index = HIDPP10Message(data).device_index
        if device_index not in hidpp20_devices:
            LOG.info(f"Registering new device, index: %s, product_id: %s", device_index, hid_device_pair.product_id)
            hidpp20_devices.append(await HIDPP20Device.create(device_pair=hid_device_pair, device_index=device_index))
        device_count -= 1

    LOG.info(f"Found %s HID++ 2.0 devices for product %s", len(hidpp20_devices), hid_device_pair.product_id)
    return hidpp20_devices


async def get_device_count(short_dev: HIDPPDeviceChannel):
    device_count_response: HIDPP10Message = await short_dev.send_request(HIDPP10Messages.GetDeviceCount.value)
    ret = 0
    if device_count_response.feature_index == 0x81 and device_count_response.parameter(0) == 0x02:
        ret = device_count_response.parameter(2)
    return ret


async def get_battery_from_pair(pair: HIDPPDevicePair, timeout_seconds=10):
    result = dict()

    [dev.start_reader_thread() for dev in pair]
    try:
        hidpp20_devices = await asyncio.wait_for(register_devices(pair), timeout=timeout_seconds)

        for device_object in hidpp20_devices:
            LOG.info(device_object)
            result[device_object] = await device_object.get_battery()
    finally:
        [dev.stop_reader_thread() for dev in pair]
    return result