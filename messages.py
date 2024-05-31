from dataclasses import dataclass
from typing import ClassVar, List

from consts import HIDPPConstants


@dataclass
class HIDPPMessageBase:
    header_length: ClassVar[int] = 0
    data: bytes

    def __init__(self, data: bytes):
        assert len(data) in (HIDPPConstants.ShortMessageLength.value, HIDPPConstants.LongMessageLength.value)
        self.data = data

    @property
    def report_id(self) -> int:
        """
        :return: The report id of the message
        """
        return self.data[0]

    @property
    def device_index(self) -> int:
        """
        :return: The device index to route the message to
        """
        return self.data[1]

    @property
    def feature_index(self) -> int:
        """
        :note: This is called a sub_id in the HID++ 1.0 documentation
        :return: A feature identifier, groups device functions
        """
        return self.data[2]

    @property
    def parameters(self) -> bytes:
        """
        :return: Parameters for device function
        """
        return self.data[self.header_length:]

    def parameter(self, parameter_index: int) -> int:
        """
        Retrieve a specific parameter by index
        :param parameter_index: The index of the parameter
        :return: A specific parameter value
        """
        assert len(self.data) - self.header_length > parameter_index >= 0
        return self.parameters[parameter_index]

    def __len__(self) -> int:
        """
        :return: The number of bytes in the message
        """
        return len(self.data)

    def __str__(self) -> str:
        """
        :return: A hex string representation of the message
        """
        return self.data.hex(' ')


@dataclass
class HIDPP10Message(HIDPPMessageBase):
    """
    A HID++ 1.0 protocol message
    """
    header_length: ClassVar[int] = 3

    def __init__(self, data: bytes):
        super().__init__(data)

    @classmethod
    def create(cls, device_index: int, sub_id: int, parameters: List[int], is_long: bool) -> 'HIDPP10Message':
        parameters = parameters if parameters else [0x00] * (HIDPPConstants.report_id(is_long) - cls.header_length)
        data = bytes([HIDPPConstants.report_id(is_long), device_index, sub_id] + parameters)
        return cls(data)


@dataclass
class HIDPP20Message(HIDPPMessageBase):
    """
    A HID++ 2.0 protocol message
    """
    header_length: ClassVar[int] = 4

    def __init__(self, data: bytes):
        super().__init__(data)

    @classmethod
    def create(cls, device_index: int, feature_index: int, function_id: int, parameters: List[int],
               is_long: bool, software_id: int = HIDPPConstants.SoftwareID.value) -> 'HIDPP20Message':
        """
        Creates a new HID++ 2.0 message
        :param device_index: Device identifier
        :param feature_index: Feature identifier
        :param function_id: Feature function identifier
        :param software_id: Communicating software ID, allows discriminating responses sent by this program
        :param is_long: True if this is a long HID++ message, otherwise False
        :param parameters: Message parameters
        """

        data = [HIDPPConstants.LongReportID.value if is_long else HIDPPConstants.ShortReportID.value, device_index,
                feature_index, (function_id << 4) | software_id]
        assert len(bytes(data)) == cls.header_length, "Invalid header length"

        expected_message_length = HIDPPConstants.LongMessageLength.value if is_long else \
            HIDPPConstants.ShortMessageLength.value
        if not parameters:
            parameters = [0x00] * (expected_message_length - cls.header_length)
        assert len(bytes(parameters)) == (expected_message_length - cls.header_length), "Invalid parameter length"
        data += parameters

        return cls(bytes(data))

    @property
    def device_index(self):
        """
        A device identifier, multiple devices may communicate with the same physical device (e.g. unified receiver)
        :return:
        """
        return self.data[1]

    @property
    def function_id(self):
        """
        A function is composed of a request sent by the host followed by one or more responses returned by the device.
        Within a given feature, each function is defined by a function identifier.
        Most functions will be read or write functions (functions which do both or neither are allowed).
        Unless otherwise specified, the protocol is big-endian.
        :return:
        """
        return (self.data[3] & 0xF0) >> 4

    @property
    def software_id(self):
        """
        :return: A number uniquely defining the software that sends a message. The firmware must copy the software identifier
                 in the response but does not use it in any other ways.
        """
        return self.data[3] & 0x0F
