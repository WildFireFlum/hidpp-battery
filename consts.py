from enum import Enum


class HIDPPConstants(Enum):
    """
    Fixed HID++ Messages used for device enumeration
    """
    SoftwareID = 0x0A
    ShortMessageLength = 7
    LongMessageLength = 20
    ShortReportID = 0x10
    LongReportID = 0x11
    HIDPP10InvalidSubID = 0x8F
    HIDShortDevUsage = 1
    HIDLongDevUsage = 2
    VendorId = 0x046d

    @staticmethod
    def report_id(is_long: bool) -> int:
        """
        A report ID for an HID++ message given the message type
        :param is_long: True for a long report, otherwise False
        """
        return HIDPPConstants.LongReportID.value if is_long else HIDPPConstants.ShortReportID.value