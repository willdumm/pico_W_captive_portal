import uerrno
import uio
import uselect as select
import usocket as socket
import gc
import network
from collections import namedtuple

from .credentials import Creds
from .filepath import rel_path
from .server import Server



WriteConn = namedtuple("WriteConn", ["body", "buff", "buffmv", "write_range"])
ReqInfo = namedtuple("ReqInfo", ["type", "path", "params", "host"])

def get_open_networks():
    sta_if = network.WLAN(network.STA_IF)
    sta_if.active(False)
    sta_if.active(True)
    ssids=[]
    for _ in range(10):
        ssids.extend(sorted(ssid.decode("utf-8") for ssid, *_ in sta_if.scan()))
        if len(ssids) > 0:
            break
    while sta_if.active():
        sta_if.active(False)
        time.sleep(1)
    print(f"Found {len(ssids)} networks")
    return ssids


def unquote(string):
    """stripped down implementation of urllib.parse unquote_to_bytes"""

    if not string:
        return b''

    if isinstance(string, str):
        string = string.encode('utf-8')

    # split into substrings on each escape character
    bits = string.split(b'%')
    if len(bits) == 1:
        return string  # there was no escape character
    
    res = [bits[0]]  # everything before the first escape character

    # for each escape character, get the next two digits and convert to 
    for item in bits[1:]:
        code = item[:2]
        char = bytes([int(code, 16)])  # convert to utf-8-encoded byte
        res.append(char)  # append the converted character
        res.append(item[2:])  # append anything else that occurred before the next escape character
    
    return b''.join(res)


class HTTPServer(Server):
    def __init__(self, poller, local_ip):
        super().__init__(poller, 80, socket.SOCK_STREAM, "HTTP Server")
        if type(local_ip) is bytes:
            self.local_ip = local_ip
        else:
            self.local_ip = local_ip.encode()
        self.request = dict()
        self.conns = dict()
        self.routes = {b"/": self.ssid_select_page, b"/login": self.login}

        self.ssid = None

        # queue up to 5 connection requests before refusing
        self.sock.listen(5)
        self.sock.setblocking(False)

    def set_ip(self, new_ip, new_ssid):
        """update settings after connected to local WiFi"""

        self.local_ip = new_ip.encode()
        self.ssid = new_ssid
        self.routes = {b"/": self.connected}

    @micropython.native
    def handle(self, sock, event, others):
        if sock is self.sock:
            # client connecting on port 80, so spawn off a new
            # socket to handle this connection
            print("- Accepting new HTTP connection")
            self.accept(sock)
        elif event & select.POLLIN:
            # socket has data to read in
            print("- Reading incoming HTTP data")
            self.read(sock)
        elif event & select.POLLOUT:
            # existing connection has space to send more data
            print("- Sending outgoing HTTP data")
            self.write_to(sock)

    def accept(self, server_sock):
        """accept a new client request socket and register it for polling"""

        try:
            client_sock, addr = server_sock.accept()
        except OSError as e:
            if e.args[0] == uerrno.EAGAIN:
                return

        client_sock.setblocking(False)
        client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.poller.register(client_sock, select.POLLIN)

    def parse_request(self, req):
        """parse a raw HTTP request to get items of interest"""

        req_lines = req.split(b"\r\n")
        req_type, full_path, http_ver = req_lines[0].split(b" ")
        path = full_path.split(b"?")
        base_path = path[0]
        query = path[1] if len(path) > 1 else None
        query_params = (
            {
                key: val
                for key, val in [param.split(b"=") for param in query.split(b"&")]
            }
            if query
            else {}
        )
        host = [line.split(b": ")[1] for line in req_lines if b"Host:" in line][0]

        return ReqInfo(req_type, base_path, query_params, host)

    def login(self, params):
        ssid = unquote(params.get(b"ssid", None))
        password = unquote(params.get(b"password", None))

        # Write out credentials
        Creds(ssid=ssid, password=password).write()

        headers = (
            b"HTTP/1.1 307 Temporary Redirect\r\n"
            b"Location: http://{:s}\r\n".format(self.local_ip)
        )

        return b"", headers

    def ssid_select_page(self, params):
        headers = b"HTTP/1.1 200 OK\r\n"
        ssids = get_open_networks()
        formatted_ssids = [f'<option value="{ssid}">{ssid}</option>'
                           for ssid in ssids]
        with open(rel_path("index.html"), "rb") as fh:
            body = fh.read().format(ssid_list="\n".join(formatted_ssids))
        return body, headers

    def connected(self, params):
        headers = b"HTTP/1.1 200 OK\r\n"
        with open(rel_path("connected.html"), "rb") as fh:
            body = fh.read() % (self.ssid, self.local_ip)
        return body, headers

    def get_response(self, req):
        """generate a response body and headers, given a route"""

        headers = b"HTTP/1.1 200 OK\r\n"
        route = self.routes.get(req.path, None)

        if type(route) is bytes:
            # expect a filename, so return contents of file
            with open(route, "rb") as fh:
                body = fh.read()
        elif callable(route):
            # call a function, which may or may not return a response
            response = route(req.params)
            body = response[0] or b""
            headers = response[1] or headers
        else:
            headers = b"HTTP/1.1 404 Not Found\r\n"
            body = b""
        return uio.BytesIO(body), headers

    def is_valid_req(self, req):
        if req.host != self.local_ip:
            # force a redirect to the MCU's IP address
            return False
        # redirect if we don't have a route for the requested path
        return req.path in self.routes

    def read(self, s):
        """read in client request from socket"""

        data = s.read()
        if not data:
            # no data in the TCP stream, so close the socket
            self.close(s)
            return

        # add new data to the full request
        sid = id(s)
        self.request[sid] = self.request.get(sid, b"") + data

        # check if additional data expected
        if data[-4:] != b"\r\n\r\n":
            # HTTP request is not finished if no blank line at the end
            # wait for next read event on this socket instead
            return

        # get the completed request
        req = self.parse_request(self.request.pop(sid))

        if not self.is_valid_req(req):
            headers = (
                b"HTTP/1.1 307 Temporary Redirect\r\n"
                b"Location: http://{:s}/\r\n".format(self.local_ip)
            )
            body = uio.BytesIO(b"")
            self.prepare_write(s, body, headers)
            return

        # by this point, we know the request has the correct
        # host and a valid route
        body, headers = self.get_response(req)
        self.prepare_write(s, body, headers)

    def prepare_write(self, s, body, headers):
        # add newline to headers to signify transition to body
        headers += "\r\n"
        # TCP/IP MSS is 536 bytes, so create buffer of this size and
        # initially populate with header data
        buff = bytearray(headers + "\x00" * (536 - len(headers)))
        # use memoryview to read directly into the buffer without copying
        buffmv = memoryview(buff)
        # start reading body data into the memoryview starting after
        # the headers, and writing at most the remaining space of the buffer
        # return the number of bytes written into the memoryview from the body
        bw = body.readinto(buffmv[len(headers) :], 536 - len(headers))
        # save place for next write event
        c = WriteConn(body, buff, buffmv, [0, len(headers) + bw])
        self.conns[id(s)] = c
        # let the poller know we want to know when it's OK to write
        self.poller.modify(s, select.POLLOUT)

    def write_to(self, sock):
        """write the next message to an open socket"""

        # get the data that needs to be written to this socket
        c = self.conns[id(sock)]
        if c:
            # write next 536 bytes (max) into the socket
            try:
                bytes_written = sock.write(c.buffmv[c.write_range[0] : c.write_range[1]])
            except OSError:
                print('cannot write to a closed socket')
                return
            if not bytes_written or c.write_range[1] < 536:
                # either we wrote no bytes, or we wrote < TCP MSS of bytes
                # so we're done with this connection
                self.close(sock)
            else:
                # more to write, so read the next portion of the data into
                # the memoryview for the next send event
                self.buff_advance(c, bytes_written)

    def buff_advance(self, c, bytes_written):
        """advance the writer buffer for this connection to next outgoing bytes"""

        if bytes_written == c.write_range[1] - c.write_range[0]:
            # wrote all the bytes we had buffered into the memoryview
            # set next write start on the memoryview to the beginning
            c.write_range[0] = 0
            # set next write end on the memoryview to length of bytes
            # read in from remainder of the body, up to TCP MSS
            c.write_range[1] = c.body.readinto(c.buff, 536)
        else:
            # didn't read in all the bytes that were in the memoryview
            # so just set next write start to where we ended the write
            c.write_range[0] += bytes_written

    def close(self, s):
        """close the socket, unregister from poller, and delete connection"""

        s.close()
        self.poller.unregister(s)
        sid = id(s)
        if sid in self.request:
            del self.request[sid]
        if sid in self.conns:
            del self.conns[sid]
        gc.collect()
