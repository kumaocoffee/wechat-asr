"""Observed WeType 3.0.0.34 wire subset; not Tencent's original protocol."""
from dataclasses import dataclass
import struct


class ProtocolError(ValueError):
    pass


def varint(value):
    if not 0 <= value < 1 << 64:
        raise ProtocolError('整数超出 protobuf uint64 范围。')
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def integer(field, value):
    return varint(field << 3) + varint(value)


def blob(field, value):
    return varint((field << 3) | 2) + varint(len(value)) + value


def fields(data):
    """Decode bounded protobuf fields, retaining unknown scalar fields."""
    if len(data) > 4 * 1024 * 1024:
        raise ProtocolError('响应过大。')
    position = 0
    result = {}

    def read_varint():
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ProtocolError('varint 不完整。')
            byte = data[position]
            position += 1
            if shift == 63 and byte > 1:
                raise ProtocolError('varint 溢出。')
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ProtocolError('varint 过长。')

    while position < len(data):
        tag = read_varint()
        field, wire = tag >> 3, tag & 7
        if not 0 < field < 1 << 29:
            raise ProtocolError('无效的字段号。')
        if wire == 0:
            value = read_varint()
        elif wire in (1, 2, 5):
            length = read_varint() if wire == 2 else (8 if wire == 1 else 4)
            if length > len(data) - position:
                raise ProtocolError('字段数据不完整。')
            value = data[position:position + length]
            position += length
        else:
            raise ProtocolError('不支持的 wire type。')
        result.setdefault(field, []).append((wire, value))
    return result


def value(message, field, wire, default):
    entries = message.get(field)
    if not entries:
        return default
    if len(entries) != 1 or entries[0][0] != wire:
        raise ProtocolError(f'字段 {field} 的类型或数量不符。')
    return entries[0][1]


def opus_packets(encoded, frames_per_packet=250):
    """Preserve one stream header and split only at 20 ms frame boundaries."""
    if frames_per_packet <= 0:
        raise ProtocolError('无效的分片帧数。')
    if encoded[:16] != b'#!OPUS_RAW_V1\x02\x01\x00':
        raise ProtocolError('不支持的 Opus 流头。')
    position, boundaries = 16, [16]
    while position < len(encoded):
        if len(encoded) - position < 2:
            raise ProtocolError('Opus 帧长度不完整。')
        size = struct.unpack_from('<H', encoded, position)[0]
        position += size + 2
        if not 0 < size <= 1500 or position > len(encoded):
            raise ProtocolError('Opus 帧不完整。')
        boundaries.append(position)
    frame_count = len(boundaries) - 1
    if not frame_count:
        raise ProtocolError('Opus 流没有音频帧。')
    packets = [encoded[0 if start == 0 else boundaries[start]:boundaries[min(start + frames_per_packet, frame_count)]]
               for start in range(0, frame_count, frames_per_packet)]
    return packets, frame_count


def request(voice_id, audio, sequence, total_bytes, *, end=False):
    # Leave optional paragraph/rewrite switches unset. In this build, forcing
    # field 22 without the complete UI option context produced reordered text
    # and no end_flag. The minimal ASR path returns an explicit final flag.
    inner = (blob(2, voice_id.encode('ascii')) + blob(4, audio) + integer(5, 5)
             + integer(6, int(end)) + integer(7, sequence)
             + integer(11, total_bytes)
             + blob(25, blob(1, b'WeType')))
    return blob(1, inner)


@dataclass(frozen=True)
class Response:
    voice_id: str
    text: str
    sequence: int
    end_flag: int
    interval_ms: int


def response(data, expected_voice_id):
    try:
        outer = fields(data)
        payload = value(outer, 1, 2, None)
        if payload is None:
            raise ProtocolError('缺少业务响应。')
        inner = fields(payload)
        base_payload = value(inner, 1, 2, None)
        if base_payload is None:
            raise ProtocolError('缺少业务状态。')
        base = fields(base_payload)
        ret = value(base, 1, 0, 0)
        if ret:
            raise ProtocolError(f'输入法业务返回错误 {ret}。')
        voice_id = value(inner, 2, 2, b'').decode('ascii')
        if voice_id != expected_voice_id:
            raise ProtocolError('响应会话标识不匹配。')
        return Response(voice_id, value(inner, 4, 2, b'').decode('utf-8'),
                        value(inner, 3, 0, 0), value(inner, 5, 0, 0),
                        value(inner, 6, 0, 100))
    except UnicodeError as error:
        raise ProtocolError('响应文字编码不正确。') from error
