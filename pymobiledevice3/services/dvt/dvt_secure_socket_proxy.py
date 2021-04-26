import dataclasses
import io
import ipaddress
import logging
import plistlib
from datetime import datetime
from functools import partial
from pprint import pprint

import IPython
from bpylist2 import archiver
from construct import Struct, Int32ul, this, Adapter, Switch, Int8ul, Bytes, Int16ub
from pygments import highlight, lexers, formatters

from pymobiledevice3.exceptions import DvtDirListError, ConnectionFailedError
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.services.dvt.structs import MessageAux, dtx_message_payload_header_struct, \
    dtx_message_header_struct, message_aux_t_struct
from pymobiledevice3.services.dvt.tap import Tap

SHELL_USAGE = '''
# This shell allows you to send messages to the DVTSecureSocketProxy and receive answers easily.
# Generally speaking, each channel represents a group of actions.
# Calling actions is done using a selector and auxiliary (parameters).
# Receiving answers is done by getting a return value and seldom auxiliary (private / extra parameters).
# To see the available channels, type the following:
developer.channels

# In order to send messages, you need to create a channel:
channel = developer.make_channel('com.apple.instruments.server.services.deviceinfo')

# After creating the channel you can call allowed selectors:
channel.runningProcesses()

# If an answer is expected, you can receive it using the receive method:
processes = channel.receive()

# Sometimes the selector requires parameters, You can add them using MessageAux. For example lets kill a process:
channel = developer.make_channel('com.apple.instruments.server.services.processcontrol')
args = MessageAux().append_obj(80) # This will kill pid 80
channel.killPid_(args, expects_reply=False) # Killing a process doesn't require an answer.

# In some rare cases, you might want to receive the auxiliary and the selector return value.
# For that cases you can use the recv_message method.
return_value, auxiliary = developer.recv_message()
'''


class DTSysmonTapMessage:
    @staticmethod
    def decode_archive(archive_obj):
        return archive_obj.decode('DTTapMessagePlist')


class NSNull:
    @staticmethod
    def decode_archive(archive_obj):
        return None


archiver.update_class_map({'DTSysmonTapMessage': DTSysmonTapMessage,
                           'DTTapHeartbeatMessage': DTSysmonTapMessage,
                           'DTTapStatusMessage': DTSysmonTapMessage,
                           'DTKTraceTapMessage': DTSysmonTapMessage,
                           'NSNull': NSNull})


class IpAddressAdapter(Adapter):
    def _decode(self, obj, context, path):
        return ipaddress.ip_address(obj)


address_t = Struct(
    'len' / Int8ul,
    'family' / Int8ul,
    'port' / Int16ub,
    'data' / Switch(this.len, {
        0x1c: Struct(
            'flow_info' / Int32ul,
            'address' / IpAddressAdapter(Bytes(16)),
            'scope_id' / Int32ul,
        ),
        0x10: Struct(
            'address' / IpAddressAdapter(Bytes(4)),
            '_zero' / Bytes(8)
        )
    })

)

MESSAGE_TYPE_INTERFACE_DETECTION = 0
MESSAGE_TYPE_CONNECTION_DETECTION = 1
MESSAGE_TYPE_CONNECTION_UPDATE = 2


@dataclasses.dataclass
class InterfaceDetectionEvent:
    interface_index: int
    name: str


@dataclasses.dataclass
class ConnectionDetectionEvent:
    local_address: str
    remote_address: str
    interface_index: int
    pid: int
    recv_buffer_size: int
    recv_buffer_used: int
    serial_number: int
    kind: int


@dataclasses.dataclass
class ConnectionUpdateEvent:
    rx_packets: int
    rx_bytes: int
    tx_bytes: int
    rx_dups: int
    rx000: int
    tx_retx: int
    min_rtt: int
    avg_rtt: int
    connection_serial: int


class Channel(int):
    @classmethod
    def create(cls, value: int, service):
        channel = cls(value)
        channel._service = service
        return channel

    def receive(self):
        return self._service.recv_message()[0]

    @staticmethod
    def _sanitize_name(name: str):
        """
        Sanitize python name to ObjectiveC name.
        """
        if name.startswith('_'):
            name = '_' + name[1:].replace('_', ':')
        else:
            name = name.replace('_', ':')
        return name

    def __getattr__(self, item):
        return partial(self._service.send_message, self, self._sanitize_name(item))


class DvtSecureSocketProxyService(object):
    INSTRUMENTS_MESSAGE_TYPE = 2
    EXPECTS_REPLY_MASK = 0x1000
    DEVICEINFO_IDENTIFIER = 'com.apple.instruments.server.services.deviceinfo'
    APP_LISTING_IDENTIFIER = 'com.apple.instruments.server.services.device.applictionListing'
    PROCESS_CONTROL_IDENTIFIER = 'com.apple.instruments.server.services.processcontrol'

    def __init__(self, lockdown: LockdownClient):
        self.logger = logging.getLogger(__name__)
        self.lockdown = lockdown

        try:
            # iOS >= 14.0
            self.service = self.lockdown.start_service('com.apple.instruments.remoteserver.DVTSecureSocketProxy')
        except ConnectionFailedError:
            # iOS < 14.0
            self.service = self.lockdown.start_service('com.apple.instruments.remoteserver')
            if hasattr(self.service.socket, '_sslobj'):
                # after the remoteserver protocol is successfully paired, you need to close the ssl protocol
                # channel and use clear text transmission
                self.service.socket._sslobj = None
        self.supported_identifiers = {}
        self.last_channel_code = 0
        self.cur_message = 0
        self.channels = {}

    def shell(self):
        IPython.embed(
            header=highlight(SHELL_USAGE, lexers.PythonLexer(), formatters.TerminalTrueColorFormatter(style='native')),
            user_ns={
                'developer': self,
                'MessageAux': MessageAux,
            })

    def ls(self, path: str):
        """
        List a directory.
        :param path: Directory to list.
        :return: Contents of the directory.
        :rtype: list[str]
        """
        channel = self.make_channel(self.DEVICEINFO_IDENTIFIER)
        args = MessageAux().append_obj(path)
        self.send_message(
            channel, 'directoryListingForPath:', args
        )
        ret, aux = self.recv_message()
        if ret is None:
            raise DvtDirListError()
        return ret

    def execname_for_pid(self, pid: int) -> str:
        """
        get full path for given pid
        :param pid: process pid
        """
        channel = self.make_channel(self.DEVICEINFO_IDENTIFIER)
        args = MessageAux().append_int(pid)
        self.send_message(
            channel, 'execnameForPid:', args
        )
        ret, aux = self.recv_message()
        return ret

    def proclist(self):
        """
        Get the process list from the device.
        :return: List of process and their attributes.
        :rtype: list[dict]
        """
        channel = self.make_channel(self.DEVICEINFO_IDENTIFIER)
        self.send_message(channel, 'runningProcesses')
        ret, aux = self.recv_message()
        assert isinstance(ret, list)
        for process in ret:
            if 'startDate' in process:
                process['startDate'] = datetime.fromtimestamp(process['startDate'])
        return ret

    def applist(self):
        """
        Get the applications list from the device.
        :return: List of applications and their attributes.
        :rtype: list[dict]
        """
        channel = self.make_channel(self.APP_LISTING_IDENTIFIER)
        args = MessageAux().append_obj({}).append_obj('')
        self.send_message(channel, 'installedApplicationsMatching:registerUpdateToken:', args)
        ret, aux = self.recv_message()
        assert isinstance(ret, list)
        return ret

    def kill(self, pid: int):
        """
        Kill a process.
        :param pid: PID of process to kill.
        """
        channel = self.make_channel(self.PROCESS_CONTROL_IDENTIFIER)
        self.send_message(channel, 'killPid:', MessageAux().append_obj(pid), False)

    def launch(self, bundle_id: str, arguments=None, kill_existing: bool = True, start_suspended: bool = False) -> int:
        """
        Launch a process.
        :param bundle_id: Bundle id of the process.
        :param list arguments: List of argument to pass to process.
        :param kill_existing: Whether to kill an existing instance of this process.
        :param start_suspended: Same as WaitForDebugger.
        :return: PID of created process.
        """
        arguments = [] if arguments is None else arguments
        channel = self.make_channel(self.PROCESS_CONTROL_IDENTIFIER)
        args = MessageAux().append_obj('').append_obj(bundle_id).append_obj({}).append_obj(arguments).append_obj({
            'StartSuspendedKey': start_suspended,
            'KillExisting': kill_existing,
        })
        self.send_message(
            channel, 'launchSuspendedProcessWithDevicePath:bundleIdentifier:environment:arguments:options:', args
        )
        ret, aux = self.recv_message()
        assert ret
        return ret

    def system_information(self):
        return self._request_information('systemInformation')

    def hardware_information(self):
        return self._request_information('hardwareInformation')

    def network_information(self):
        return self._request_information('networkInformation')

    def network_monitor(self):
        channel = self.make_channel('com.apple.instruments.server.services.networking')
        channel.startMonitoring(expects_reply=False)

        while True:
            message, _ = self.recv_message()

            event = None

            if message is None:
                continue

            if message[0] == MESSAGE_TYPE_INTERFACE_DETECTION:
                event = InterfaceDetectionEvent(*message[1])
            elif message[0] == MESSAGE_TYPE_CONNECTION_DETECTION:
                event = ConnectionDetectionEvent(*message[1])
                event.local_address = address_t.parse(event.local_address)
                event.remote_address = address_t.parse(event.remote_address)
            elif message[0] == MESSAGE_TYPE_CONNECTION_UPDATE:
                event = ConnectionUpdateEvent(*message[1])

            try:
                yield event
            finally:
                channel.stopMonitoring()

    def sysmontap(self):
        return Tap(self, 'com.apple.instruments.server.services.sysmontap', {
            'ur': 1000,  # Output frequency ms
            'bm': 0,
            'procAttrs': self.process_attributes,
            'sysAttrs': self.system_attributes,
            'cpuUsage': True,
            'sampleInterval': 1000000000})

    def perform_handshake(self):
        args = MessageAux()
        args.append_obj({'com.apple.private.DTXBlockCompression': 0, 'com.apple.private.DTXConnection': 1})
        self.send_message(0, '_notifyOfPublishedCapabilities:', args, expects_reply=False)
        ret, aux = self.recv_message()
        if ret != '_notifyOfPublishedCapabilities:':
            raise ValueError('Invalid answer')
        if not len(aux[0]):
            raise ValueError('Invalid answer')
        self.supported_identifiers = aux[0].value

    def make_channel(self, identifier):
        assert identifier in self.supported_identifiers
        if identifier in self.channels:
            return self.channels[identifier]

        self.last_channel_code += 1
        code = self.last_channel_code
        args = MessageAux().append_int(code).append_obj(identifier)
        self.send_message(0, '_requestChannelWithCode:identifier:', args)
        ret, aux = self.recv_message()
        assert ret is None
        channel = Channel.create(code, self)
        self.channels[identifier] = channel
        return channel

    def send_message(self, channel: int, selector: str = None, args: MessageAux = None, expects_reply: bool = True):
        self.cur_message += 1

        aux = bytes(args) if args is not None else b''
        sel = archiver.archive(selector) if selector is not None else b''
        flags = self.INSTRUMENTS_MESSAGE_TYPE
        if expects_reply:
            flags |= self.EXPECTS_REPLY_MASK
        pheader = dtx_message_payload_header_struct.build(dict(flags=flags, auxiliaryLength=len(aux),
                                                               totalLength=len(aux) + len(sel)))
        mheader = dtx_message_header_struct.build(dict(
            cb=dtx_message_header_struct.sizeof(),
            fragmentId=0,
            fragmentCount=1,
            length=dtx_message_payload_header_struct.sizeof() + len(aux) + len(sel),
            identifier=self.cur_message,
            conversationIndex=0,
            channelCode=channel,
            expectsReply=int(expects_reply)
        ))
        msg = mheader + pheader + aux + sel
        self.service.sendall(msg)

    def recv_message(self):
        packet_stream = self._recv_packet_fragments()
        pheader = dtx_message_payload_header_struct.parse_stream(packet_stream)

        compression = (pheader.flags & 0xFF000) >> 12
        if compression:
            raise NotImplementedError('Compressed')

        if pheader.auxiliaryLength:
            aux = message_aux_t_struct.parse_stream(packet_stream).aux
        else:
            aux = None
        obj_size = pheader.totalLength - pheader.auxiliaryLength
        data = packet_stream.read(obj_size)
        ret = None
        if data:
            try:
                ret = archiver.unarchive(data) if obj_size else None
            except archiver.MissingClassMapping as e:
                pprint(plistlib.loads(data))
                raise e
            except plistlib.InvalidFileException:
                logging.warning(f'got an invalid plist: {data[:40]}')
        return ret, aux

    def _request_information(self, selector_name):
        channel = self.make_channel(self.DEVICEINFO_IDENTIFIER)
        self.send_message(channel, selector_name)
        ret, aux = self.recv_message()
        assert ret
        return ret

    def _recv_packet_fragments(self):
        packet_data = b''
        while True:
            data = self.service.recvall(dtx_message_header_struct.sizeof())
            mheader = dtx_message_header_struct.parse(data)
            if not mheader.conversationIndex:
                if mheader.identifier > self.cur_message:
                    self.cur_message = mheader.identifier
            if mheader.fragmentCount > 1 and mheader.fragmentId == 0:
                # when reading multiple message fragments, the first fragment contains only a message header
                continue
            packet_data += self.service.recvall(mheader.length)
            if mheader.fragmentId == mheader.fragmentCount - 1:
                break
        return io.BytesIO(packet_data)

    def __enter__(self):
        self.perform_handshake()

        # query device for its "queryable" attributes
        self.process_attributes = list(self._request_information('sysmonProcessAttributes'))
        self.system_attributes = list(self._request_information('sysmonSystemAttributes'))

        self.SysmonProcAttributes = dataclasses.make_dataclass('SysmonProcAttributes',
                                                               [f.replace('_', '') for f in self.process_attributes])
        self.SysmonSystemAttributes = dataclasses.make_dataclass('SysmonSystemAttributes',
                                                                 [f.replace('_', '') for f in self.system_attributes])
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass