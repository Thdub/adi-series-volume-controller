#Requires AutoHotkey v2.0
; ============================================================================
; ADI_Series_Volume_Controller.ahk
; Hotkey-driven volume control for RME ADI-2 Pro via MIDI SysEx.
; IR codes generated using https://github.com/Thdub/generate_rme_broadlink_ir
; ============================================================================

; =============================== CONFIG ====================================
; --- Device ---
device_id := "72"   ; "71" -> ADI-2 DAC, "72" -> ADI-2 Pro, "73" -> ADI-2/4 Pro SE

; --- Mode ---
default_output  := "1/2"   ; "1/2" -> Output 1/2 | "3/4" -> Output 3/4
split_mode      := false   ; false -> single/cloned device | true -> Device#1 on default_output, Device#2 on second output
; Note: In split_mode, each device controls its output, click toggles Mute instead of switching selected output,
; long click - if available - will swap/invert assigned output between devices.
; You will need to set different hotkeys for your second device.

; --- Controls ---
default_db_out12    := -41.5    ; value in dB
default_db_out34    := -30.0    ; value in dB
danger_db_threshold := -6.0     ; at/above this (louder), any move resets to default instead of applying it
step_normal_db      := 0.1      ; normal steps (value in dB)
fast_threshold_ms   := 180      ; ticks closer together than this = fast spin (value in ms)
step_fast_db        := 0.5      ; fast spin steps (value in dB)
step_click_db       := 1.5      ; click + rotation (value in dB)

; --- Hotkeys ---
; Edit the key codes below to match your own device/keyboard shortcuts,
; then copy & paste the code before "::".
; eg: <!SC099::AdjustVolume(-1)

!F13::AdjustVolume(-1)   ; left -> volume down (-0.1dB default)
!F14::AdjustVolume(1)    ; right -> volume up (+0.1dB default)
!F15::SingleClick()      ; click -> switch Output 1/2 and 3-4 (in default mode) or mute output (in split mode)

; Extended actions for devices supporting more than 3 input states (ignore the values below if unused)
F16::AdjustVolumeCoarse(-1)   ; click+left -> coarse step down (-1.5dB default)
F17::AdjustVolumeCoarse(1)    ; click+right -> coarse step up (+1.5dB default)
F18::LongClick()              ; long click -> mute or swap devices depending on split_mode

; --- Split Mode Hotkeys (Device #2) ---
^!+F20::AdjustVolumeDevice2(-1)       ; left -> volume down (-0.1dB default)
^!+F21::AdjustVolumeDevice2(1)        ; right -> volume up (+0.1dB default)
^!+F22::SingleClickDevice2()          ; click -> mute selected output
; Extended actions for devices supporting more than 3 input states (ignore the values below if unused)
^!+F23::AdjustVolumeCoarseDevice2(-1) ; click+left -> coarse step down (-1.5dB default)
^!+F24::AdjustVolumeCoarseDevice2(1)  ; click+right -> coarse step up (+1.5dB default)

; --- Background resync (catches drift from changes made outside this ----
; script, e.g. the front-panel encoder or the ADI-2 Remote app)
resync_interval_minutes := 30   ; 0 = disable

; --- Midi ---
send_midi_exe    := A_ScriptDir "\sendmidi.exe"
receive_midi_exe := A_ScriptDir "\receivemidi.exe"
; Retrieve your exact port name by running this command: sendmidi.exe list
midi_port_name   := "ADI-2 Pro Midi Port 1"

; --- Broadlink IR (optional, syncs the LCD "volume select" indicator) ----
; Requires a Broadlink hub taught the remote's "VOL Push" command.
; Set use_broadlink to false to skip entirely (no paths or hardware infos needed then).
use_broadlink       := false
python_exe          := "python"

; Install: pip install broadlink
; Run discovery: python -c "import broadlink; [print(f'Device: {d.devtype:#06x} | IP: {d.host[0]} | MAC: {d.mac.hex()}') for d in broadlink.discover()]"
broadlink_type      := "0x649b"
broadlink_host      := ""
broadlink_mac       := ""

; --------------------------- PROTOCOL CONSTANTS -----------------------------
; RME
; Same across the RME ADI-2 family for shared SysEx functions (channel volume/mute).
; RME SysEx addresses: Output 1/2 = 0x03, Output 3/4 = 0x09
; You can double-check with RME_Sniff_SysEx.ps1 in case your model behaves differently.
addr_out12  := 3
addr_out34  := 9
idx_volume  := 12
idx_mute    := 15

; Broadlink IR - VOL Push (LCD output-select indicator),
; generated mathematically from the RME IR command tables + NEC protocol
; (not a physical scan - identical for anyone with the same model).
broadlink_adi2_vol_push    := "26004800000128941212123712121212123712121212121212121212123712121237123712121212121212371237123712121237121212121237121212121212123712121237123712000521"
broadlink_adi2pro_vol_push := "26004800000128941212123712121212123712121212121212121212123712121237123712121212121212371237123712121237121212371237121212121212123712121237121212000521"
broadlink_adi24_vol_push   := "26004800000128941212123712121212123712121212121212121212123712121237123712121212121212371237123712121237123712121237121212121212123712121212123712000521"

; ------------------------------ DERIVED VALUES -------------------------------
default_db_out12_x10    := Round(default_db_out12 * 10)
default_db_out34_x10    := Round(default_db_out34 * 10)
danger_db_threshold_x10 := Round(danger_db_threshold * 10)
step_normal_x10         := Round(step_normal_db * 10)
step_fast_x10           := Round(step_fast_db * 10)
step_coarse_x10         := Round(step_click_db * 10)

; Pick the right IR code for the configured device_id
broadlink_vol_select_code := (device_id = "71") ? broadlink_adi2_vol_push
    : (device_id = "73") ? broadlink_adi24_vol_push
    : broadlink_adi2pro_vol_push

; ------------------------------ STATE (RAM) ----------------------------------

State := Map(
    "activeOutput", default_output,
    "dbOut12", default_db_out12_x10,
    "dbOut34", default_db_out34_x10,
    "mutedOut12", false,
    "mutedOut34", false,
    "lastTickdev1", 0,
    "lastTickdev2", 0
)

; ------------------------------ INIT -----------------------------------------

SyncAllFromDevice()

if (resync_interval_minutes > 0)
    SetTimer(SyncAllFromDevice, resync_interval_minutes * 60000)

; ------------------------------ LOGIC -----------------------------------------

GetDev2Output() {
    global split_mode, State

    if (!split_mode)
        return State["activeOutput"]

    return (State["activeOutput"] = "1/2") ? "3/4" : "1/2"
}

AdjustVolume(direction) {
    global State, fast_threshold_ms, step_normal_x10, step_fast_x10

    now := A_TickCount
    delta := now - State["lastTickdev1"]
    State["lastTickdev1"] := now

    step := (delta > 0 && delta < fast_threshold_ms)
        ? step_fast_x10
        : step_normal_x10

    _ApplyVolumeStep(State["activeOutput"], direction, step)
}

AdjustVolumeCoarse(direction) {
    global State, step_coarse_x10

    State["lastTickdev1"] := A_TickCount
    _ApplyVolumeStep(State["activeOutput"], direction, step_coarse_x10)
}

AdjustVolumeDevice2(direction) {
    global State, fast_threshold_ms, step_normal_x10, step_fast_x10

    target := GetDev2Output()

    now := A_TickCount
    delta := now - State["lastTickdev2"]
    State["lastTickdev2"] := now

    step := (delta > 0 && delta < fast_threshold_ms)
        ? step_fast_x10
        : step_normal_x10

    _ApplyVolumeStep(target, direction, step)
}

AdjustVolumeCoarseDevice2(direction) {
    global State, step_coarse_x10

    target := GetDev2Output()
    State["lastTickdev2"] := A_TickCount

    _ApplyVolumeStep(target, direction, step_coarse_x10)
}

_ApplyVolumeStep(targetOutput, direction, stepX10) {
    global State
    global danger_db_threshold_x10
    global default_db_out12_x10, default_db_out34_x10
    global addr_out12, addr_out34, idx_volume, idx_mute

    dbKey := (targetOutput = "1/2")
        ? "dbOut12"
        : "dbOut34"

    mutedKey := (targetOutput = "1/2")
        ? "mutedOut12"
        : "mutedOut34"

    addr := (targetOutput = "1/2")
        ? addr_out12
        : addr_out34

    defaultDb := (targetOutput = "1/2")
        ? default_db_out12_x10
        : default_db_out34_x10

    ; Changing the volume automatically unmutes that output.
    if (State[mutedKey]) {
        State[mutedKey] := false
        SendChannelParam(addr, idx_mute, 0)
    }

    current := State[dbKey]

    ; At/above the danger threshold, any move resets to the configured
    ; default instead of applying the increment - a quick flick in either
    ; direction is enough to get back to a safe level.
    if (current >= danger_db_threshold_x10)
        newVal := defaultDb
    else
        newVal := current + (direction * stepX10)

    State[dbKey] := newVal
    SendChannelParam(addr, idx_volume, newVal)
}

SingleClick() {
    global split_mode

    if (split_mode)
        ToggleMute()
    else
        SwitchOutput()
}

LongClick() {
    global split_mode, State, use_broadlink

    if (split_mode) {
        ; Device #1 and Device #2 swap their assigned outputs.
        State["activeOutput"] := (State["activeOutput"] = "1/2")
            ? "3/4"
            : "1/2"

        if (use_broadlink)
            SendBroadlinkVolumeSelect()
    }
    else {
        ToggleMute()
    }
}

SwitchOutput() {
    global State, use_broadlink

    State["activeOutput"] := (State["activeOutput"] = "1/2")
        ? "3/4"
        : "1/2"

    if (use_broadlink)
        SendBroadlinkVolumeSelect()
}

SingleClickDevice2() {
    global State
    ToggleMuteForOutput(GetDev2Output())
}

ToggleMute() {
    global State
    ToggleMuteForOutput(State["activeOutput"])
}

ToggleMuteForOutput(targetOutput) {
    global State, addr_out12, addr_out34, idx_mute

    mutedKey := (targetOutput = "1/2")
        ? "mutedOut12"
        : "mutedOut34"

    addr := (targetOutput = "1/2")
        ? addr_out12
        : addr_out34

    State[mutedKey] := !State[mutedKey]

    SendChannelParam(
        addr,
        idx_mute,
        State[mutedKey] ? 1 : 0
    )
}

SendBroadlinkVolumeSelect() {
    global python_exe
    global broadlink_type
    global broadlink_host
    global broadlink_mac
    global broadlink_vol_select_code

    py := "import broadlink; from broadlink.const import DEFAULT_PORT; "
        . "dev = broadlink.gendevice(" broadlink_type ", ('" broadlink_host "', DEFAULT_PORT), bytearray.fromhex('" broadlink_mac "')); "
        . "dev.auth(); "
        . "dev.send_data(bytearray.fromhex('" broadlink_vol_select_code "'))"

    Run('"' python_exe '" -c "' py '"', , "Hide")
}

; ------------------------------ SYSEX ENCODING --------------------------------
; Formula verified byte-for-byte against RME's own examples.

ByteToHex(b) {
    static hex_chars := "0123456789ABCDEF"

    return SubStr(hex_chars, ((b >> 4) & 0x0F) + 1, 1)
        . SubStr(hex_chars, (b & 0x0F) + 1, 1)
}

EncodeChannelParam(address, idx, val) {
    v := val & 0xFFF

    b1 := ((address & 0x0F) << 3)
        | ((idx >> 2) & 0x07)

    b2 := ((idx & 0x03) << 5)
        | (((v >> 11) & 0x01) << 4)
        | ((v >> 7) & 0x0F)

    b3 := v & 0x7F

    return [
        ByteToHex(b1),
        ByteToHex(b2),
        ByteToHex(b3)
    ]
}

DecodeChannelContainer(b1, b2, b3) {
    address := (b1 >> 3) & 0x0F

    idx := ((b1 & 0x07) << 2)
        | ((b2 >> 5) & 0x03)

    v := (((b2 >> 4) & 0x01) << 11)
        | ((b2 & 0x0F) << 7)
        | (b3 & 0x7F)

    if (v & 0x800)
        v -= 4096

    return {
        address: address,
        idx: idx,
        value: v
    }
}

SendChannelParam(address, idx, val) {
    bytes := EncodeChannelParam(address, idx, val)

    SendRawHex(
        bytes[1] " "
        bytes[2] " "
        bytes[3]
    )
}

SendRawHex(hexBytes) {
    global send_midi_exe, midi_port_name, device_id

    RunWait(
        '"' send_midi_exe '"'
        ' dev "' midi_port_name '"'
        ' system-exclusive hex'
        ' 00 20 0D ' device_id ' 02 '
        hexBytes,
        ,
        "Hide"
    )
}

; ------------------------------ STARTUP / PERIODIC SYNC -----------------------
; Single "send all settings" request, short listen window, then the input
; port is closed - the device stops broadcasting once nobody is listening.

SyncAllFromDevice() {
    global State
    global send_midi_exe, receive_midi_exe, midi_port_name, device_id
    global addr_out12, addr_out34, idx_volume, idx_mute

    tempFile := A_Temp "\rme_sync_" A_TickCount ".txt"

    try {
        ; Start listening BEFORE requesting the current settings so the
        ; response cannot be missed.
        Run(
            '"' A_ComSpec '" /c ""'
            receive_midi_exe
            '" dev "' midi_port_name
            '" > "' tempFile
            '""',
            ,
            "Hide"
        )

        Sleep 100

        ; Request the device to send its current settings.
        RunWait(
            '"' send_midi_exe '"'
            ' dev "' midi_port_name '"'
            ' system-exclusive hex'
            ' 00 20 0D ' device_id ' 03 09',
            ,
            "Hide"
        )

        ; Short listening window.
        Sleep 1200

        ; Stop the temporary receiver.
        try RunWait(
            '"' A_ComSpec '" /c taskkill /IM receivemidi.exe /F',
            ,
            "Hide"
        )

        Sleep 100

        if !FileExist(tempFile)
            return

        content := FileRead(tempFile)

        tokens := []

        for m in StrSplit(content, [" ", "`r", "`n"], " ") {
            t := Trim(m)

            if RegExMatch(t, "^[0-9A-Fa-f]{2}$")
                tokens.Push(Integer("0x" t))
        }

        deviceIdInt := Integer("0x" device_id)

        i := 1

        while (i <= tokens.Length - 4) {
            if (
                tokens[i] = 0x00
                && tokens[i + 1] = 0x20
                && tokens[i + 2] = 0x0D
                && tokens[i + 3] = deviceIdInt
            ) {
                cmd := tokens[i + 4]
                p := i + 5

                if (cmd = 0x01 || cmd = 0x02) {
                    while (p + 2 <= tokens.Length) {
                        b1 := tokens[p]
                        b2 := tokens[p + 1]
                        b3 := tokens[p + 2]

                        ; 0x0C is not a channel container we use here.
                        if (((b1 >> 3) & 0x0F) != 12) {
                            dec := DecodeChannelContainer(b1, b2, b3)

                            if (
                                dec.address = addr_out12
                                && dec.idx = idx_volume
                            ) {
                                State["dbOut12"] := dec.value
                            }
                            else if (
                                dec.address = addr_out12
                                && dec.idx = idx_mute
                            ) {
                                State["mutedOut12"] := (dec.value != 0)
                            }
                            else if (
                                dec.address = addr_out34
                                && dec.idx = idx_volume
                            ) {
                                State["dbOut34"] := dec.value
                            }
                            else if (
                                dec.address = addr_out34
                                && dec.idx = idx_mute
                            ) {
                                State["mutedOut34"] := (dec.value != 0)
                            }

                        }

                        p += 3
                    }
                }
            }

            i += 1
        }

        try FileDelete(tempFile)
    }
    catch {
        try FileDelete(tempFile)
    }
}
