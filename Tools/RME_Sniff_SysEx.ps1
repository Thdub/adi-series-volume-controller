<#
    RME_Sniff_SysEx.ps1  (v2 - automatic decoder)
    -----------------------------------------------
    Listens on the ADI-2 Pro's MIDI port and automatically DECODES every
    SysEx parameter it receives (address / index / value), based on RME's
    own official protocol document (MIDITable_ADI-2_230930.ods).

    Formula confirmed against 2 official RME examples (checked byte by byte):

    CHANNEL parameters (address 0-11, 13, 14) - signed 12-bit value:
      byte1 = (address << 3) | (idx >> 2 & 0x07)
      byte2 = ((idx & 0x03) << 5) | (bit11_of_value << 4) | ((value >> 7) & 0x0F)
      byte3 = value & 0x7F

    DEVICE parameters (fixed address = 12) - unsigned 11-bit value:
      byte1 = (12 << 3) | (idx >> 3 & 0x07)
      byte2 = ((idx & 0x07) << 4) | ((value >> 7) & 0x0F)
      byte3 = value & 0x7F

    Useful channel addresses: 3 = Line Out (1-2), 6 = Phones 12, 9 = Phones 34
    Useful channel indexes  : 12 = Volume (dB x10), 15 = Mute (0/1)

    USAGE:
      .\RME_Sniff_SysEx.ps1                    -> passive listening, decodes live
      .\RME_Sniff_SysEx.ps1 -RequestAll        -> requests every setting at startup
                                                    (equivalent to the .ahk "startup sync")
      .\RME_Sniff_SysEx.ps1 -DeviceId 71        -> ADI-2 DAC (default: 72, ADI-2 Pro;
                                                    73 = ADI-2/4 Pro SE)

    Make one change AT A TIME on the physical device, noting what you did
    (e.g. "turned the headphone knob one notch") - the decoded line right
    after gives the exact address/index to report back into
    ADI_Series_Volume_Controller.ahk (or rme_app.py) if they differ from the
    ones already configured (address 3 = Line, 9 = Phones -> already wired
    into the main app).
#>

param(
    [string]$ReceiveMidiPath = (Join-Path $PSScriptRoot "receivemidi.exe"),
    [string]$SendMidiPath    = (Join-Path $PSScriptRoot "sendmidi.exe"),
    [string]$PortName        = "ADI-2 Pro Midi Port 1",
    [string]$DeviceId        = "72",    # 71 = ADI-2 DAC, 72 = ADI-2 Pro, 73 = ADI-2/4 Pro SE
    [switch]$RequestAll
)

$DeviceIdInt = [Convert]::ToInt32($DeviceId, 16)

function Decode-Container {
    param([int]$b1, [int]$b2, [int]$b3)

    $address = ($b1 -shr 3) -band 0x0F

    if ($address -eq 12) {
        # "Device" parameter: 6-bit index, unsigned 11-bit value
        $idxHi3 = $b1 -band 0x07
        $idxLo3 = ($b2 -shr 4) -band 0x07
        $idx    = ($idxHi3 -shl 3) -bor $idxLo3
        $val    = (($b2 -band 0x0F) -shl 7) -bor ($b3 -band 0x7F)
        return "DEVICE   idx=$idx   value=$val (unsigned)"
    }
    else {
        # "Channel" parameter: 5-bit index, signed 12-bit value
        $idxHi3   = $b1 -band 0x07
        $idxLo2   = ($b2 -shr 5) -band 0x03
        $idx      = ($idxHi3 -shl 2) -bor $idxLo2
        $bit11    = ($b2 -shr 4) -band 0x01
        $bits10_7 = $b2 -band 0x0F
        $val      = ($bit11 -shl 11) -bor ($bits10_7 -shl 7) -bor ($b3 -band 0x7F)
        if ($val -band 0x800) { $val = $val - 4096 }

        $label = switch ($address) {
            3 { "Line Out (1-2)" }
            6 { "Phones 12" }
            9 { "Phones 34" }
            default { "address $address" }
        }
        $paramLabel = switch ($idx) {
            12 { "Volume ($([math]::Round($val/10,1)) dB)" }
            15 { "Mute ($val)" }
            default { "idx $idx" }
        }
        return "$label  ->  $paramLabel   [addr=$address idx=$idx raw_value=$val]"
    }
}

function Parse-Line {
    param([string]$line, [int]$deviceIdInt)

    if ($line -notmatch $DeviceId) { return }

    $tokens = ($line -split '\s+') | Where-Object { $_ -match '^[0-9A-Fa-f]{2}$' }
    if (-not $tokens) { return }
    $bytes = $tokens | ForEach-Object { [Convert]::ToInt32($_, 16) }

    for ($i = 0; $i -lt $bytes.Count - 4; $i++) {
        if ($bytes[$i] -eq 0x00 -and $bytes[$i+1] -eq 0x20 -and $bytes[$i+2] -eq 0x0D -and $bytes[$i+3] -eq $deviceIdInt) {
            $cmd = $bytes[$i+4]
            $p = $i + 5

            if ($cmd -eq 0x01 -or $cmd -eq 0x02) {
                $tag = if ($cmd -eq 0x01) { "DEVICE->US" } else { "US->DEVICE" }
                while ($p + 2 -lt $bytes.Count) {
                    $desc = Decode-Container $bytes[$p] $bytes[$p+1] $bytes[$p+2]
                    Write-Host "  [$tag] $desc" -ForegroundColor Green
                    $p += 3
                }
            }
            elseif ($cmd -eq 0x07) {
                Write-Host "  [STATUS 1s] (cmd 0x07 - byte 6 holds the active output)" -ForegroundColor DarkGray
            }
            elseif ($cmd -eq 0x03) {
                Write-Host "  [REQUEST sent, sub-command 0x$('{0:X2}' -f $bytes[$p])]" -ForegroundColor Yellow
            }
            break
        }
    }
}

if (-not (Test-Path $ReceiveMidiPath)) {
    Write-Host "receivemidi.exe not found at: $ReceiveMidiPath" -ForegroundColor Red
    Write-Host "Same author as sendmidi -> https://github.com/gbevin/ReceiveMIDI/releases"
    exit 1
}

Write-Host "=== Available MIDI ports ===" -ForegroundColor Cyan
& $ReceiveMidiPath list
Write-Host ""

if ($RequestAll) {
    Write-Host "Sending the 'all settings' request (00 20 0D $DeviceId 03 09)..." -ForegroundColor Cyan
    & $SendMidiPath dev $PortName system-exclusive hex 00 20 0D $DeviceId 03 09
    Write-Host ""
}

Write-Host "=== Listening on '$PortName' (Ctrl+C to stop) ===" -ForegroundColor Cyan
Write-Host "Every parameter received is decoded automatically below." -ForegroundColor Cyan
Write-Host ""

& $ReceiveMidiPath dev $PortName | ForEach-Object {
    Write-Host $_ -ForegroundColor DarkGray
    Parse-Line $_ $DeviceIdInt
}