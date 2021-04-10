#!/usr/bin/env python3

import argparse
import functools
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import Dict, List, Optional, TextIO, Tuple, Union
from dataclasses import dataclass

from bdflib import reader as bdfreader
from bdflib.model import Font, Glyph

import font_tables

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

HERE = Path(__file__).resolve().parent


@functools.lru_cache(maxsize=None)
def cjk_font() -> Font:
    with open(os.path.join(HERE, "wqy-bitmapsong/wenquanyi_9pt.bdf"), "rb") as f:
        return bdfreader.read_bdf(f)


# Loading a single JSON file
def load_json(filename: str, skip_first_line: bool) -> dict:
    with open(filename) as f:
        if skip_first_line:
            f.readline()
        return json.loads(f.read())


def read_translation(json_root: Union[str, Path], lang_code: str) -> dict:
    filename = f"translation_{lang_code}.json"

    file_with_path = os.path.join(json_root, filename)

    try:
        lang = load_json(file_with_path, skip_first_line=False)
    except json.decoder.JSONDecodeError as e:
        logging.error(f"Failed to decode {filename}")
        logging.exception(str(e))
        sys.exit(2)

    validate_langcode_matches_content(filename, lang)

    return lang


def validate_langcode_matches_content(filename: str, content: dict) -> None:
    # Extract lang code from file name
    lang_code = filename[12:-5].upper()
    # ...and the one specified in the JSON file...
    try:
        lang_code_from_json = content["languageCode"]
    except KeyError:
        lang_code_from_json = "(missing)"

    # ...cause they should be the same!
    if lang_code != lang_code_from_json:
        raise ValueError(
            f"Invalid languageCode {lang_code_from_json} in file {filename}"
        )


def write_start(f: TextIO):
    f.write(
        "// WARNING: THIS FILE WAS AUTO GENERATED BY make_translation.py. PLEASE DO NOT EDIT.\n"
    )
    f.write("\n")
    f.write('#include "Translation.h"\n')


def get_constants(build_version: str) -> List[Tuple[str, str]]:
    # Extra constants that are used in the firmware that are shared across all languages
    return [
        ("SymbolPlus", "+"),
        ("SymbolMinus", "-"),
        ("SymbolSpace", " "),
        ("SymbolDot", "."),
        ("SymbolDegC", "C"),
        ("SymbolDegF", "F"),
        ("SymbolMinutes", "M"),
        ("SymbolSeconds", "S"),
        ("SymbolWatts", "W"),
        ("SymbolVolts", "V"),
        ("SymbolDC", "DC"),
        ("SymbolCellCount", "S"),
        ("SymbolVersionNumber", build_version),
    ]


def get_debug_menu() -> List[str]:
    return [
        datetime.today().strftime("%d-%m-%y"),
        "HW G ",
        "HW M ",
        "HW P ",
        "Time ",
        "Move ",
        "RTip ",
        "CTip ",
        "CHan ",
        "Vin  ",
        "PCB  ",
        "PWR  ",
        "Max  ",
    ]


def get_letter_counts(defs: dict, lang: dict, build_version: str) -> List[str]:
    text_list = []
    # iterate over all strings
    obj = lang["menuOptions"]
    for mod in defs["menuOptions"]:
        eid = mod["id"]
        text_list.append(obj[eid]["desc"])

    obj = lang["messages"]
    for mod in defs["messages"]:
        eid = mod["id"]
        if eid not in obj:
            text_list.append(mod["default"])
        else:
            text_list.append(obj[eid])

    obj = lang["messagesWarn"]
    for mod in defs["messagesWarn"]:
        eid = mod["id"]
        if isinstance(obj[eid], list):
            text_list.append(obj[eid][0])
            text_list.append(obj[eid][1])
        else:
            text_list.append(obj[eid])

    obj = lang["characters"]

    for mod in defs["characters"]:
        eid = mod["id"]
        text_list.append(obj[eid])

    obj = lang["menuOptions"]
    for mod in defs["menuOptions"]:
        eid = mod["id"]
        if isinstance(obj[eid]["text2"], list):
            text_list.append(obj[eid]["text2"][0])
            text_list.append(obj[eid]["text2"][1])
        else:
            text_list.append(obj[eid]["text2"])

    obj = lang["menuGroups"]
    for mod in defs["menuGroups"]:
        eid = mod["id"]
        if isinstance(obj[eid]["text2"], list):
            text_list.append(obj[eid]["text2"][0])
            text_list.append(obj[eid]["text2"][1])
        else:
            text_list.append(obj[eid]["text2"])

    obj = lang["menuGroups"]
    for mod in defs["menuGroups"]:
        eid = mod["id"]
        text_list.append(obj[eid]["desc"])
    constants = get_constants(build_version)
    for x in constants:
        text_list.append(x[1])
    text_list.extend(get_debug_menu())

    # collapse all strings down into the composite letters and store totals for these

    symbol_counts: dict[str, int] = {}
    for line in text_list:
        line = line.replace("\n", "").replace("\r", "")
        line = line.replace("\\n", "").replace("\\r", "")
        if line:
            for letter in line:
                symbol_counts[letter] = symbol_counts.get(letter, 0) + 1
    # swap to Big -> little sort order
    symbols_by_occurrence = [
        x[0] for x in sorted(symbol_counts.items(), key=lambda kv: (kv[1], kv[0]))
    ]
    symbols_by_occurrence.reverse()
    return symbols_by_occurrence


def get_cjk_glyph(sym: str) -> str:
    glyph: Glyph = cjk_font()[ord(sym)]

    data = glyph.data
    src_left, src_bottom, src_w, src_h = glyph.get_bounding_box()
    dst_w = 12
    dst_h = 16

    # The source data is a per-row list of ints. The first item is the bottom-
    # most row. For each row, the LSB is the right-most pixel.
    # Here, (x, y) is the coordinates with origin at the top-left.
    def get_cell(x: int, y: int) -> bool:
        # Adjust x coordinates by actual bounding box.
        adj_x = x - src_left
        if adj_x < 0 or adj_x >= src_w:
            return False
        # Adjust y coordinates by actual bounding box, then place the glyph
        # baseline 3px above the bottom edge to make it centre-ish.
        # This metric is optimized for WenQuanYi Bitmap Song 9pt and assumes
        # each glyph is to be placed in a 12x12px box.
        adj_y = y - (dst_h - src_h - src_bottom - 3)
        if adj_y < 0 or adj_y >= src_h:
            return False
        if data[src_h - adj_y - 1] & (1 << (src_w - adj_x - 1)):
            return True
        else:
            return False

    # A glyph in the font table is divided into upper and lower parts, each by
    # 8px high. Each byte represents half if a column, with the LSB being the
    # top-most pixel. The data goes from the left-most to the right-most column
    # of the top half, then from the left-most to the right-most column of the
    # bottom half.
    s = ""
    for block in range(2):
        for c in range(dst_w):
            b = 0
            for r in range(8):
                if get_cell(c, r + 8 * block):
                    b |= 0x01 << r
            s += f"0x{b:02X},"
    return s


def get_bytes_from_font_index(index: int) -> bytes:
    """
    Converts the font table index into its corresponding bytes
    """

    # We want to be able to use more than 254 symbols (excluding \x00 null
    # terminator and \x01 new-line) in the font table but without making all
    # the chars take 2 bytes. To do this, we use \xF1 to \xFF as lead bytes
    # to designate double-byte chars, and leave the remaining as single-byte
    # chars.
    #
    # For the sake of sanity, \x00 always means the end of string, so we skip
    # \xF1\x00 and others in the mapping.
    #
    # Mapping example:
    #
    # 0x02 => 2
    # 0x03 => 3
    # ...
    # 0xEF => 239
    # 0xF0 => 240
    # 0xF1 0x01 => 1 * 0xFF - 15 + 1 = 241
    # 0xF1 0x02 => 1 * 0xFF - 15 + 2 = 242
    # ...
    # 0xF1 0xFF => 1 * 0xFF - 15 + 255 = 495
    # 0xF2 0x01 => 2 * 0xFF - 15 + 1 = 496
    # ...
    # 0xF2 0xFF => 2 * 0xFF - 15 + 255 = 750
    # 0xF3 0x01 => 3 * 0xFF - 15 + 1 = 751
    # ...
    # 0xFF 0xFF => 15 * 0xFF - 15 + 255 = 4065

    if index < 0:
        raise ValueError("index must be positive")
    page = (index + 0x0E) // 0xFF
    if page > 0x0F:
        raise ValueError("page value out of range")
    if page == 0:
        return bytes([index])
    else:
        # Into extended range
        # Leader is 0xFz where z is the page number
        # Following char is the remainder
        leader = page + 0xF0
        value = ((index + 0x0E) % 0xFF) + 0x01

        if leader > 0xFF or value > 0xFF:
            raise ValueError("value is out of range")
        return bytes([leader, value])


def bytes_to_escaped(b: bytes) -> str:
    return "".join((f"\\x{i:02X}" for i in b))


@dataclass
class FontMap:
    font12: Dict[str, str]
    font06: Dict[str, str]


def get_font_map_and_table(
    text_list: List[str],
) -> Tuple[List[str], FontMap, Dict[str, bytes]]:
    # the text list is sorted
    # allocate out these in their order as number codes
    symbol_map: Dict[str, bytes] = {"\n": bytes([1])}
    index = 2  # start at 2, as 0= null terminator,1 = new line
    forced_first_symbols = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]

    # Get the font table, which does not include CJK chars
    font_table = font_tables.get_font_map()
    font_small_table = font_tables.get_small_font_map()

    # We want to put all CJK chars after non-CJK ones so that the CJK chars
    # do not need to be in the small font table to save space.
    # We assume all symbols not in the font table to be a CJK char.
    # We also enforce that numbers are first.
    ordered_normal_sym_list: List[str] = forced_first_symbols + [
        x for x in text_list if x not in forced_first_symbols and x in font_table
    ]
    ordered_cjk_sym_list: List[str] = [
        x for x in text_list if x not in forced_first_symbols and x not in font_table
    ]

    total_symbol_count = len(ordered_normal_sym_list) + len(ordered_cjk_sym_list)
    # \x00 is for NULL termination and \x01 is for newline, so the maximum
    # number of symbols allowed is as follow (see also the comments in
    # `get_bytes_from_font_index`):
    if total_symbol_count > (0x10 * 0xFF - 15) - 2:  # 4063
        logging.error(
            f"Error, too many used symbols for this version (total {total_symbol_count})"
        )
        sys.exit(1)

    logging.info(f"Generating fonts for {total_symbol_count} symbols")

    sym_list = ordered_normal_sym_list + ordered_cjk_sym_list
    for sym in sym_list:
        if sym in symbol_map:
            raise ValueError("Symbol not found in symbol map")
        symbol_map[sym] = get_bytes_from_font_index(index)
        index += 1

    font12_map: Dict[str, str] = {}
    font06_map: Dict[str, str] = {}
    for sym in ordered_normal_sym_list:
        if sym not in font_table:
            logging.error(f"Missing Large font element for {sym}")
            sys.exit(1)
        font12_map[sym] = font_table[sym]
        if sym not in font_small_table:
            logging.error(f"Missing Small font element for {sym}")
            sys.exit(1)
        font06_map[sym] = font_small_table[sym]

    for sym in ordered_cjk_sym_list:
        if sym in font_table:
            raise ValueError("Symbol already exists in font_table")
        font_line: str = get_cjk_glyph(sym)
        if font_line is None:
            logging.error(f"Missing Large font element for {sym}")
            sys.exit(1)
        font12_map[sym] = font_line
        # No data to add to the small font table
        font06_map[sym] = "//                                 "  # placeholder

    return sym_list, FontMap(font12_map, font06_map), symbol_map


def make_font_table_cpp(
    sym_list: List[str], font_map: FontMap, symbol_map: Dict[str, bytes]
) -> str:
    output_table = "const uint8_t USER_FONT_12[] = {\n"
    for sym in sym_list:
        output_table += (
            f"{font_map.font12[sym]}//{bytes_to_escaped(symbol_map[sym])} -> {sym}\n"
        )
    output_table += "};\n"

    output_table += "const uint8_t USER_FONT_6x8[] = {\n"
    for sym in sym_list:
        output_table += (
            f"{font_map.font06[sym]}//{bytes_to_escaped(symbol_map[sym])} -> {sym}\n"
        )
    output_table += "};\n"
    return output_table


def convert_string_bytes(symbol_conversion_table: Dict[str, bytes], text: str) -> bytes:
    # convert all of the symbols from the string into bytes for their content
    output_string = b""
    for c in text.replace("\\r", "").replace("\\n", "\n"):
        if c not in symbol_conversion_table:
            logging.error(f"Missing font definition for {c}")
            sys.exit(1)
        else:
            output_string += symbol_conversion_table[c]
    return output_string


def convert_string(symbol_conversion_table: Dict[str, bytes], text: str) -> str:
    # convert all of the symbols from the string into escapes for their content
    return bytes_to_escaped(convert_string_bytes(symbol_conversion_table, text))


def escape(string: str) -> str:
    return json.dumps(string, ensure_ascii=False)


@dataclass
class TranslationItem:
    info: str
    str_index: int


def write_language(lang: dict, defs: dict, build_version: str, f: TextIO) -> None:
    language_code: str = lang["languageCode"]
    logging.info(f"Generating block for {language_code}")
    # Iterate over all of the text to build up the symbols & counts
    text_list = get_letter_counts(defs, lang, build_version)
    # From the letter counts, need to make a symbol translator & write out the font
    sym_list, font_map, symbol_conversion_table = get_font_map_and_table(text_list)
    font_table_text = make_font_table_cpp(sym_list, font_map, symbol_conversion_table)

    try:
        lang_name = lang["languageLocalName"]
    except KeyError:
        lang_name = language_code

    f.write(f"\n// ---- {lang_name} ----\n\n")
    f.write(font_table_text)
    f.write(f"\n// ---- {lang_name} ----\n\n")

    translation_common_text = get_translation_common_text(
        defs, symbol_conversion_table, build_version
    )
    f.write(translation_common_text)
    f.write(
        f"const bool HasFahrenheit = {('true' if lang.get('tempUnitFahrenheit', True) else 'false')};\n\n"
    )

    translation_strings_and_indices_text = get_translation_strings_and_indices_text(
        lang, defs, symbol_conversion_table
    )
    f.write(translation_strings_and_indices_text)
    f.write("const TranslationIndexTable *const Tr = &TranslationIndices;\n")
    f.write("const char *const TranslationStrings = TranslationStringsData;\n\n")

    sanity_checks_text = get_translation_sanity_checks_text(defs)
    f.write(sanity_checks_text)


def get_translation_common_text(
    defs: dict, symbol_conversion_table: Dict[str, bytes], build_version
) -> str:
    translation_common_text = ""

    # Write out firmware constant options
    constants = get_constants(build_version)
    for x in constants:
        translation_common_text += f'const char* {x[0]} = "{convert_string(symbol_conversion_table, x[1])}";//{x[1]} \n'
    translation_common_text += "\n"

    # Debug Menu
    translation_common_text += "const char* DebugMenu[] = {\n"

    for c in get_debug_menu():
        translation_common_text += (
            f'\t "{convert_string(symbol_conversion_table, c)}",//{c} \n'
        )
    translation_common_text += "};\n\n"
    return translation_common_text


def get_translation_strings_and_indices_text(
    lang: dict, defs: dict, symbol_conversion_table: Dict[str, bytes]
) -> str:
    str_table: List[str] = []
    str_group_messages: List[TranslationItem] = []
    str_group_messageswarn: List[TranslationItem] = []
    str_group_characters: List[TranslationItem] = []
    str_group_settingdesc: List[TranslationItem] = []
    str_group_settingshortnames: List[TranslationItem] = []
    str_group_settingmenuentries: List[TranslationItem] = []
    str_group_settingmenuentriesdesc: List[TranslationItem] = []

    eid: str

    # ----- Reading SettingsDescriptions
    obj = lang["menuOptions"]

    for index, mod in enumerate(defs["menuOptions"]):
        eid = mod["id"]
        str_group_settingdesc.append(
            TranslationItem(f"[{index:02d}] {eid}", len(str_table))
        )
        str_table.append(obj[eid]["desc"])

    # ----- Reading Message strings

    obj = lang["messages"]

    for mod in defs["messages"]:
        eid = mod["id"]
        source_text = ""
        if "default" in mod:
            source_text = mod["default"]
        if eid in obj:
            source_text = obj[eid]
        str_group_messages.append(TranslationItem(eid, len(str_table)))
        str_table.append(source_text)

    obj = lang["messagesWarn"]

    for mod in defs["messagesWarn"]:
        eid = mod["id"]
        if isinstance(obj[eid], list):
            if not obj[eid][1]:
                source_text = obj[eid][0]
            else:
                source_text = obj[eid][0] + "\n" + obj[eid][1]
        else:
            source_text = "\n" + obj[eid]
        str_group_messageswarn.append(TranslationItem(eid, len(str_table)))
        str_table.append(source_text)

    # ----- Reading Characters

    obj = lang["characters"]

    for mod in defs["characters"]:
        eid = mod["id"]
        str_group_characters.append(TranslationItem(eid, len(str_table)))
        str_table.append(obj[eid])

    # ----- Reading SettingsDescriptions
    obj = lang["menuOptions"]

    for index, mod in enumerate(defs["menuOptions"]):
        eid = mod["id"]
        if isinstance(obj[eid]["text2"], list):
            if not obj[eid]["text2"][1]:
                source_text = obj[eid]["text2"][0]
            else:
                source_text = obj[eid]["text2"][0] + "\n" + obj[eid]["text2"][1]
        else:
            source_text = "\n" + obj[eid]["text2"]
        str_group_settingshortnames.append(
            TranslationItem(f"[{index:02d}] {eid}", len(str_table))
        )
        str_table.append(source_text)

    # ----- Reading Menu Groups
    obj = lang["menuGroups"]

    for index, mod in enumerate(defs["menuGroups"]):
        eid = mod["id"]
        if isinstance(obj[eid]["text2"], list):
            if not obj[eid]["text2"][1]:
                source_text = obj[eid]["text2"][0]
            else:
                source_text = obj[eid]["text2"][0] + "\n" + obj[eid]["text2"][1]
        else:
            source_text = "\n" + obj[eid]["text2"]
        str_group_settingmenuentries.append(
            TranslationItem(f"[{index:02d}] {eid}", len(str_table))
        )
        str_table.append(source_text)

    # ----- Reading Menu Groups Descriptions
    obj = lang["menuGroups"]

    for index, mod in enumerate(defs["menuGroups"]):
        eid = mod["id"]
        str_group_settingmenuentriesdesc.append(
            TranslationItem(f"[{index:02d}] {eid}", len(str_table))
        )
        str_table.append(obj[eid]["desc"])

    @dataclass
    class RemappedTranslationItem:
        str_index: int
        str_start_offset: int = 0

    # ----- Perform suffix merging optimization:
    #
    # We sort the backward strings so that strings with the same suffix will
    # be next to each other, e.g.:
    #   "ef\0",
    #   "cdef\0",
    #   "abcdef\0",
    backward_sorted_table: List[Tuple[int, str, bytes]] = sorted(
        (
            (i, s, bytes(reversed(convert_string_bytes(symbol_conversion_table, s))))
            for i, s in enumerate(str_table)
        ),
        key=lambda x: x[2],
    )
    str_remapping: List[Optional[RemappedTranslationItem]] = [None] * len(str_table)
    for i, (str_index, source_str, converted) in enumerate(backward_sorted_table[:-1]):
        j = i
        while backward_sorted_table[j + 1][2].startswith(converted):
            j += 1
        if j != i:
            str_remapping[str_index] = RemappedTranslationItem(
                str_index=backward_sorted_table[j][0],
                str_start_offset=len(backward_sorted_table[j][2]) - len(converted),
            )

    # ----- Write the string table:
    str_offsets = [-1] * len(str_table)
    offset = 0
    write_null = False
    translation_strings_text = "const char TranslationStringsData[] = {\n"
    for i, source_str in enumerate(str_table):
        if str_remapping[i] is not None:
            continue
        if write_null:
            translation_strings_text += ' "\\0"\n'
        write_null = True
        # Find what items use this string
        str_used_by = [i] + [
            j for j, r in enumerate(str_remapping) if r and r.str_index == i
        ]
        for j in str_used_by:
            for group, pre_info in [
                (str_group_messages, "messages"),
                (str_group_messageswarn, "messagesWarn"),
                (str_group_characters, "characters"),
                (str_group_settingdesc, "SettingsDescriptions"),
                (str_group_settingshortnames, "SettingsShortNames"),
                (str_group_settingmenuentries, "SettingsMenuEntries"),
                (str_group_settingmenuentriesdesc, "SettingsMenuEntriesDescriptions"),
            ]:
                for item in group:
                    if item.str_index == j:
                        translation_strings_text += (
                            f"  //     - {pre_info} {item.info}\n"
                        )
            if j == i:
                translation_strings_text += f"  // {offset: >4}: {escape(source_str)}\n"
                str_offsets[j] = offset
            else:
                remapped = str_remapping[j]
                assert remapped is not None
                translation_strings_text += f"  // {offset + remapped.str_start_offset: >4}: {escape(str_table[j])}\n"
                str_offsets[j] = offset + remapped.str_start_offset
        converted_bytes = convert_string_bytes(symbol_conversion_table, source_str)
        translation_strings_text += f'  "{bytes_to_escaped(converted_bytes)}"'
        str_offsets[i] = offset
        # Add the length and the null terminator
        offset += len(converted_bytes) + 1
    translation_strings_text += "\n}; // TranslationStringsData\n\n"

    def get_offset(idx: int) -> int:
        assert str_offsets[idx] >= 0
        return str_offsets[idx]

    translation_indices_text = "const TranslationIndexTable TranslationIndices = {\n"

    # ----- Write the messages string indices:
    for group in [str_group_messages, str_group_messageswarn, str_group_characters]:
        for item in group:
            translation_indices_text += f"  .{item.info} = {get_offset(item.str_index)}, // {escape(str_table[item.str_index])}\n"
        translation_indices_text += "\n"

    # ----- Write the settings index tables:
    for group, name in [
        (str_group_settingdesc, "SettingsDescriptions"),
        (str_group_settingshortnames, "SettingsShortNames"),
        (str_group_settingmenuentries, "SettingsMenuEntries"),
        (str_group_settingmenuentriesdesc, "SettingsMenuEntriesDescriptions"),
    ]:
        max_len = 30
        translation_indices_text += f"  .{name} = {{\n"
        for item in group:
            translation_indices_text += f"    /* {item.info.ljust(max_len)[:max_len]} */ {get_offset(item.str_index)}, // {escape(str_table[item.str_index])}\n"
        translation_indices_text += f"  }}, // {name}\n\n"

    translation_indices_text += "}; // TranslationIndices\n\n"

    return translation_strings_text + translation_indices_text


def get_translation_sanity_checks_text(defs: dict) -> str:
    sanity_checks_text = "\n// Verify SettingsItemIndex values:\n"
    for i, mod in enumerate(defs["menuOptions"]):
        eid = mod["id"]
        sanity_checks_text += (
            f"static_assert(static_cast<uint8_t>(SettingsItemIndex::{eid}) == {i});\n"
        )
    sanity_checks_text += f"static_assert(static_cast<uint8_t>(SettingsItemIndex::NUM_ITEMS) == {len(defs['menuOptions'])});\n"
    return sanity_checks_text


def read_version() -> str:
    with open(HERE.parent / "source" / "version.h") as version_file:
        for line in version_file:
            if re.findall(r"^.*(?<=(#define)).*(?<=(BUILD_VERSION))", line):
                matches = re.findall(r"\"(.+?)\"", line)
                if matches:
                    version = matches[0]
                    try:
                        version += f".{subprocess.check_output(['git', 'rev-parse', '--short=7', 'HEAD']).strip().decode('ascii').upper()}"
                    # --short=7: the shorted hash with 7 digits. Increase/decrease if needed!
                    except OSError:
                        version += " git"
    return version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", "-o", help="Target file", type=argparse.FileType("w"), required=True
    )
    parser.add_argument("languageCode", help="Language to generate")
    return parser.parse_args()


def main() -> None:
    json_dir = HERE

    args = parse_args()
    try:
        build_version = read_version()
    except FileNotFoundError:
        logging.error("error: Could not find version info ")
        sys.exit(1)

    logging.info(f"Build version: {build_version}")
    logging.info(f"Making {args.languageCode} from {json_dir}")

    lang_ = read_translation(json_dir, args.languageCode)
    defs_ = load_json(os.path.join(json_dir, "translations_def.js"), True)
    out_ = args.output
    write_start(out_)
    write_language(lang_, defs_, build_version, out_)

    logging.info("Done")


if __name__ == "__main__":
    main()
