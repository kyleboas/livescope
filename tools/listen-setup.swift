// Sets a Mac up for LISTEN in Safari: a "Speakers + BlackHole" output that plays the Mac's sound on the speakers
// (or headphones) and copies it to BlackHole, which the page then hears as an input. No microphone involved.
// Needs BlackHole 2ch first: brew install --cask blackhole-2ch
//   swift tools/listen-setup.swift          make the output (once) and switch the Mac's sound to it
//   swift tools/listen-setup.swift off      switch the Mac's sound back to the speakers
import CoreAudio
import Foundation

let SYS = AudioObjectID(kAudioObjectSystemObject)
func addr(_ s: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: s, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}
func devices() -> [AudioDeviceID] {
    var a = addr(kAudioHardwarePropertyDevices), size: UInt32 = 0
    AudioObjectGetPropertyDataSize(SYS, &a, 0, nil, &size)
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(SYS, &a, 0, nil, &size, &ids)
    return ids
}
func text(_ id: AudioDeviceID, _ s: AudioObjectPropertySelector) -> String {
    var a = addr(s), v: Unmanaged<CFString>? = nil, size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, &v) == noErr, let v else { return "" }
    return v.takeRetainedValue() as String
}
func outputs(_ id: AudioDeviceID) -> Bool {
    var a = addr(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput), size: UInt32 = 0
    AudioObjectGetPropertyDataSize(id, &a, 0, nil, &size)
    return size > 0
}
func transport(_ id: AudioDeviceID) -> UInt32 {
    var a = addr(kAudioDevicePropertyTransportType), v: UInt32 = 0, size = UInt32(MemoryLayout<UInt32>.size)
    AudioObjectGetPropertyData(id, &a, 0, nil, &size, &v)
    return v
}
func setDefault(_ id: AudioDeviceID) {
    for s in [kAudioHardwarePropertyDefaultOutputDevice, kAudioHardwarePropertyDefaultSystemOutputDevice] {
        var a = addr(s), v = id
        AudioObjectSetPropertyData(SYS, &a, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &v)
    }
}
func fail(_ s: String) -> Never { FileHandle.standardError.write((s + "\n").data(using: .utf8)!); exit(1) }

let UID = "livescope.listen.multiout", NAME = "Speakers + BlackHole"
let all = devices()
let blackhole = all.first { text($0, kAudioDevicePropertyDeviceUID).hasPrefix("BlackHole2ch") }
// the real output to hear it on: the built-in speakers, else any output that isn't BlackHole or an aggregate
let real = all.filter { outputs($0) && transport($0) != kAudioDeviceTransportTypeAggregate && transport($0) != kAudioDeviceTransportTypeVirtual && $0 != blackhole }
let speakers = real.first { transport($0) == kAudioDeviceTransportTypeBuiltIn } ?? real.first

if CommandLine.arguments.dropFirst().first == "off" {
    guard let speakers else { fail("no speakers found") }
    setDefault(speakers); print("The Mac's sound is back on \(text(speakers, kAudioObjectPropertyName)).")
    exit(0)
}
guard let blackhole else { fail("BlackHole 2ch isn't installed. Install it first: brew install --cask blackhole-2ch") }
guard let speakers else { fail("no speakers found") }
var multi = all.first { text($0, kAudioDevicePropertyDeviceUID) == UID }
if multi == nil {
    let su = text(speakers, kAudioDevicePropertyDeviceUID), bu = text(blackhole, kAudioDevicePropertyDeviceUID)
    let desc: [String: Any] = [
        kAudioAggregateDeviceNameKey: NAME, kAudioAggregateDeviceUIDKey: UID,
        kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: su], [kAudioSubDeviceUIDKey: bu, kAudioSubDeviceDriftCompensationKey: 1]],
        kAudioAggregateDeviceMainSubDeviceKey: su,
        kAudioAggregateDeviceIsStackedKey: 1,       // a Multi-Output Device: the same sound to both
    ]
    var id: AudioObjectID = 0
    let err = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &id)
    guard err == noErr else { fail("couldn't make the \(NAME) output (CoreAudio error \(err))") }
    multi = id
    Thread.sleep(forTimeInterval: 0.5)
}
setDefault(multi!)
print("""
The Mac's sound now plays on \(text(speakers, kAudioObjectPropertyName)) and goes to BlackHole too.
In livescope: LISTEN, pick "BlackHole 2ch", Listen. Play LiveATC in any tab or app.
The volume keys don't work on this output: set the volume in LiveATC's player.
Back to normal: swift tools/listen-setup.swift off
""")
