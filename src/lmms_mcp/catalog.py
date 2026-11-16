"""Knowledge about LMMS' built-in instruments and effects.

Parameter names, defaults and ranges were taken from the LMMS sources
(``plugins/*``). Parameters that are not listed can still be set - LMMS simply
uses the plugin default for every attribute that is missing in the file.
"""

from __future__ import annotations

import base64
import math
import struct

P = tuple  # (default, min, max, description)

WAVES = "0 sine, 1 triangle, 2 saw, 3 square, 4 moog saw, 5 exponential, 6 white noise, 7 user"
FILTER_TYPES = [
    "lowpass", "highpass", "bandpass_csg", "bandpass_czpg", "notch", "allpass", "moog",
    "lowpass_x2", "rc_lowpass_12", "rc_bandpass_12", "rc_highpass_12", "rc_lowpass_24",
    "rc_bandpass_24", "rc_highpass_24", "vocal_formant", "moog_x2", "sv_lowpass",
    "sv_bandpass", "sv_highpass", "sv_notch", "fast_formant", "tripole",
]
ARP_DIRECTIONS = ["up", "down", "up_down", "down_up", "random"]
ARP_MODES = ["free", "sort", "sync"]
# Chord table of LMMS' note stacking / arpeggiator (index = stored value).
CHORD_TABLE = [
    "octave", "Major", "Majb5", "minor", "minb5", "sus2", "sus4", "aug", "augsus4", "tri",
    "6", "6sus4", "6add9", "m6", "m6add9", "7", "7sus4", "7#5", "7b5", "7#9", "7b9",
    "7#5#9", "7#5b9", "7b5b9", "7add11", "7add13", "7#11", "Maj7", "Maj7b5", "Maj7#5",
    "Maj7#11", "Maj7add13", "m7", "m7b5", "m7b9", "m7add11", "m7add13", "m-Maj7",
    "m-Maj7add11", "m-Maj7add13", "9", "9sus4", "add9", "9#5", "9b5", "9#11", "9b13",
    "Maj9", "Maj9sus4", "Maj9#5", "Maj9#11", "m9", "madd9", "m9b5", "m9-Maj7", "11",
    "11b9", "Maj11", "m11", "m-Maj11", "13", "13#9", "13b9", "13b5b9", "Maj13", "m13",
    "m-Maj13", "Major", "Harmonic minor", "Melodic minor", "Whole tone", "Diminished",
    "Major pentatonic", "Minor pentatonic", "Jap in sen", "Major bebop", "Dominant bebop",
    "Blues", "Arabic", "Enigmatic", "Neopolitan", "Neopolitan minor", "Hungarian minor",
    "Dorian", "Phrygian", "Lydian", "Mixolydian", "Aeolian", "Locrian", "Minor",
    "Chromatic", "Half-Whole Diminished", "5", "Phrygian dominant", "Persian",
]


def _tripleosc_defaults() -> dict:
    d = {}
    for i, coarse in enumerate((0, -12, -24)):
        d.update({f"vol{i}": 33, f"pan{i}": 0, f"coarse{i}": coarse, f"finel{i}": 0,
                  f"finer{i}": 0, f"phoffset{i}": 0, f"stphdetun{i}": 0, f"wavetype{i}": 0,
                  f"userwavefile{i}": ""})
    d.update({"modalgo1": 2, "modalgo2": 2, "modalgo3": 2,
              "useWaveTable1": 1, "useWaveTable2": 1, "useWaveTable3": 1})
    return d


# name -> spec. "defaults" are written into every new instrument node.
INSTRUMENTS: dict[str, dict] = {
    "tripleoscillator": {
        "title": "TripleOscillator",
        "description": "Three-oscillator subtractive synth. The all-round workhorse: basses, "
                       "leads, pads, plucks (shape it with set_envelope / set_filter).",
        "use": ["bass", "lead", "pad", "pluck", "keys"],
        "preset_dir": "TripleOscillator",
        "defaults": _tripleosc_defaults(),
        "params": {
            "wavetype0/1/2": P((0, 0, 7, f"Oscillator waveform: {WAVES}")),
            "vol0/1/2": P((33, 0, 200, "Oscillator volume")),
            "coarse0/1/2": P((0, -24, 24, "Detune in semitones (defaults 0, -12, -24)")),
            "finel0/1/2, finer0/1/2": P((0, -100, 100, "Fine detune left/right in cents (detune L/R for width)")),
            "pan0/1/2": P((0, -100, 100, "Oscillator panning")),
            "phoffset0/1/2": P((0, 0, 360, "Phase offset")),
            "stphdetun0/1/2": P((0, 0, 360, "Stereo phase detuning")),
            "modalgo1": P((2, 0, 4, "How osc1 modulates osc2: 0 PM, 1 AM, 2 mix, 3 sync, 4 FM")),
            "modalgo2": P((2, 0, 4, "How osc2 modulates osc3 (same values)")),
        },
    },
    "kicker": {
        "title": "Kicker",
        "description": "Synthesized kick / tom / percussion (pitch sweep with distortion).",
        "use": ["drums", "kick", "bass"],
        "preset_dir": "Kicker",
        "defaults": {"startfreq": 150, "endfreq": 40, "decay": 440, "dist": 0.8, "distend": 0.8,
                     "gain": 1, "env": 0.163, "noise": 0, "click": 0.4, "slope": 0.06,
                     "startnote": 0, "endnote": 0, "version": 1},
        "params": {
            "startfreq": P((150, 5, 1000, "Start frequency in Hz")),
            "endfreq": P((40, 5, 1000, "End frequency in Hz")),
            "decay": P((440, 5, 5000, "Length in ms")),
            "dist": P((0.8, 0, 100, "Start distortion")),
            "distend": P((0.8, 0, 100, "End distortion")),
            "gain": P((1, 0.1, 5, "Gain")),
            "env": P((0.163, 0.01, 1, "Amplitude envelope slope")),
            "slope": P((0.06, 0.001, 1, "Frequency slope")),
            "noise": P((0, 0, 1, "Noise amount (snares/hats)")),
            "click": P((0.4, 0, 1, "Click amount")),
            "startnote": P((0, 0, 1, "1 = start frequency follows the played note")),
            "endnote": P((0, 0, 1, "1 = end frequency follows the played note (808 style bass)")),
        },
    },
    "audiofileprocessor": {
        "title": "AudioFileProcessor",
        "description": "Sample player. Plays an audio file (wav/ogg/flac...) chromatically; "
                       "the sample's original pitch sounds at A4 (change with base_note).",
        "use": ["drums", "samples", "one-shots", "sampled instruments"],
        "preset_dir": "AudioFileProcessor",
        "defaults": {"src": "", "amp": 100, "sframe": 0, "eframe": 1, "lframe": 0,
                     "looped": 0, "reversed": 0, "interp": 1, "stutter": 0},
        "params": {
            "src": P(("", None, None, "Sample file: factory path like 'drums/kick01.ogg' or absolute path")),
            "amp": P((100, 0, 500, "Amplify in %")),
            "sframe": P((0, 0, 1, "Start point (0-1)")),
            "eframe": P((1, 0, 1, "End point (0-1)")),
            "lframe": P((0, 0, 1, "Loop point (0-1)")),
            "looped": P((0, 0, 2, "0 off, 1 loop, 2 ping-pong")),
            "reversed": P((0, 0, 1, "Play reversed")),
            "interp": P((1, 0, 2, "Interpolation: 0 none, 1 linear, 2 sinc")),
        },
    },
    "sf2player": {
        "title": "Sf2 Player",
        "description": "SoundFont (.sf2) player - realistic pianos, strings, guitars, drums if "
                       "you have a soundfont file. Needs an absolute path to a .sf2 file.",
        "use": ["piano", "orchestral", "realistic instruments"],
        "defaults": {"src": "", "patch": 0, "bank": 0, "gain": 1, "reverbOn": 0,
                     "reverbRoomSize": 0.2, "reverbDamping": 0, "reverbWidth": 0.5,
                     "reverbLevel": 0.9, "chorusOn": 0, "chorusNum": 3, "chorusLevel": 2,
                     "chorusSpeed": 0.3, "chorusDepth": 8},
        "params": {
            "src": P(("", None, None, "Path of the .sf2 file")),
            "patch": P((0, 0, 127, "Program number (General MIDI: 0 piano, 24 guitar, 32 bass, 48 strings...)")),
            "bank": P((0, 0, 999, "Bank number (128 = GM drum kits)")),
            "gain": P((1, 0, 5, "Gain")),
            "reverbOn": P((0, 0, 1, "Built-in reverb")),
            "chorusOn": P((0, 0, 1, "Built-in chorus")),
        },
    },
    "lb302": {
        "title": "LB302",
        "description": "TB-303 style monophonic acid bass synth.",
        "use": ["bass", "acid"],
        "preset_dir": "LB302",
        "defaults": {"vcf_cut": 0.75, "vcf_res": 0.75, "vcf_mod": 0.1, "vcf_dec": 0.1,
                     "dist": 0, "shape": 8, "slide_dec": 0.6, "slide": 0, "dead": 0, "db24": 0},
        "params": {
            "vcf_cut": P((0.75, 0, 1.5, "Filter cutoff")),
            "vcf_res": P((0.75, 0, 1.25, "Filter resonance")),
            "vcf_mod": P((0.1, 0, 1, "Filter envelope amount")),
            "vcf_dec": P((0.1, 0, 1, "Filter envelope decay")),
            "dist": P((0, 0, 1, "Distortion")),
            "shape": P((8, 0, 11, "Waveform: 0 saw, 1 triangle, 2 square, 3 round square, 4 moog, "
                                   "5 sine, 6 exponential, 7 noise, 8 BL saw, 9 BL square, 10 BL triangle, 11 BL moog")),
            "slide": P((0, 0, 1, "Slide (portamento) on")),
            "slide_dec": P((0.6, 0, 1, "Slide decay")),
            "dead": P((0, 0, 1, "Dead (no filter envelope)")),
            "db24": P((0, 0, 1, "24 dB/oct filter")),
        },
    },
    "bitinvader": {
        "title": "BitInvader",
        "description": "Wavetable synth with a drawable single-cycle wave - chiptune leads, "
                       "buzzy basses. Use the special param 'wave' (sine, saw, square, triangle, noise).",
        "use": ["chiptune", "lead", "bass"],
        "preset_dir": "BitInvader",
        "defaults": {"version": "0.1", "sampleLength": 128, "interpolation": 0, "normalize": 0},
        "params": {
            "wave": P(("sine", None, None, "Wave shape: sine, saw, square, triangle, noise (generated)")),
            "interpolation": P((0, 0, 1, "Smooth the wave")),
            "normalize": P((0, 0, 1, "Normalize output")),
        },
    },
    "organic": {
        "title": "Organic",
        "description": "Additive organ-like synth with 8 harmonics.",
        "use": ["organ", "pad"],
        "preset_dir": "Organic",
        "defaults": {},
        "params": {
            "vol": P((100, 0, 200, "Overall volume")),
            "foldback": P((0, 0, 0.99, "Distortion")),
            "vol0..7": P((100, 0, 100, "Volume of harmonic oscillator 0-7")),
            "wavetype0..7": P((0, 0, 5, "Waveform per oscillator")),
            "newharmonic0..7": P((None, 0, 17, "Harmonic number per oscillator")),
            "newdetune0..7": P((0, -1200, 1200, "Detune per oscillator in cents")),
            "pan0..7": P((0, -100, 100, "Pan per oscillator")),
        },
    },
    "sfxr": {
        "title": "sfxr",
        "description": "Retro game sound-effect generator (lasers, pickups, explosions, noise hats).",
        "use": ["chiptune", "fx", "drums"],
        "defaults": {},
        "params": {
            "waveForm": P((0, 0, 3, "0 square, 1 saw, 2 sine, 3 noise")),
            "att, hold, sus, dec": P((None, 0, 1, "Envelope")),
            "startFreq": P((0.3, 0, 1, "Start frequency")),
            "slide, dSlide": P((0, -1, 1, "Pitch slide / delta slide")),
            "vibDepth, vibSpeed": P((0, 0, 1, "Vibrato")),
            "sqrDuty, sqrSweep": P((0, 0, 1, "Square duty / sweep")),
            "lpFilCut, lpFilReso, hpFilCut": P((None, 0, 1, "Filters")),
        },
    },
    "monstro": {
        "title": "Monstro", "preset_dir": "Monstro",
        "description": "Large 3-oscillator modulation-matrix synth. Best used with a preset.",
        "use": ["lead", "bass", "pad"], "defaults": {}, "params": {},
    },
    "zynaddsubfx": {
        "title": "ZynAddSubFX", "preset_dir": "ZynAddSubFX",
        "description": "Huge additive/subtractive/pad synth with hundreds of factory presets "
                       "(pianos, strings, brass, pads, organs...). Use with a preset (list_presets).",
        "use": ["piano", "strings", "brass", "pad", "organ", "realistic instruments"],
        "defaults": {"portamento": 0, "filterfreq": 64, "filterq": 64, "bandwidth": 64,
                     "fmgain": 127, "rescenterfreq": 64, "resbandwidth": 64,
                     "forwardmidicc": 0, "modifiedcontrollers": ""},
        "params": {},
    },
    "opulenz": {
        "title": "OpulenZ", "preset_dir": "OpulenZ",
        "description": "2-operator FM synth (OPL2 / AdLib chip). Best used with a preset.",
        "use": ["fm", "chiptune", "keys", "bass"], "defaults": {}, "params": {},
    },
    "nes": {
        "title": "Nescaline", "preset_dir": "Nescaline",
        "description": "NES sound chip emulation (2 pulse, triangle, noise).",
        "use": ["chiptune"], "defaults": {}, "params": {},
    },
    "freeboy": {
        "title": "FreeBoy",
        "description": "Game Boy sound chip emulation.",
        "use": ["chiptune"], "defaults": {}, "params": {},
    },
    "sid": {
        "title": "SID", "preset_dir": "SID",
        "description": "Commodore 64 SID chip emulation.",
        "use": ["chiptune", "bass", "lead"], "defaults": {}, "params": {},
    },
    "watsyn": {
        "title": "Watsyn", "preset_dir": "Watsyn", "needs_preset": True,
        "description": "4-oscillator wavetable synth. Needs a preset (wave tables are stored in it).",
        "use": ["lead", "bass", "pad"], "defaults": {}, "params": {},
    },
    "vibedstrings": {
        "title": "Vibed", "preset_dir": "Vibed", "needs_preset": True,
        "description": "Vibrating string modeler. Needs a preset.",
        "use": ["plucked strings", "harpsichord"], "defaults": {}, "params": {},
    },
    "xpressive": {
        "title": "Xpressive", "preset_dir": "Xpressive", "needs_preset": True,
        "description": "Math-expression synth. Needs a preset.",
        "use": ["lead", "fx", "experimental"], "defaults": {}, "params": {},
    },
    "slicert": {
        "title": "SlicerT", "min_version": "1.3",
        "description": "Beat slicer (LMMS 1.3+ only).",
        "use": ["loops"], "defaults": {}, "params": {},
    },
}

EFFECTS: dict[str, dict] = {
    "amplifier": {
        "node": "AmplifierControls", "description": "Volume / pan / left-right gain utility.",
        "params": {"volume": P((100, 0, 200, "Volume in %")), "pan": P((0, -100, 100, "Panning")),
                   "left": P((100, 0, 200, "Left gain")), "right": P((100, 0, 200, "Right gain"))},
    },
    "bassbooster": {
        "node": "bassboostercontrols", "description": "Boosts low frequencies.",
        "params": {"freq": P((100, 50, 200, "Frequency in Hz")), "gain": P((1, 0.1, 5, "Gain")),
                   "ratio": P((2, 0.1, 10, "Ratio"))},
    },
    "bitcrush": {
        "node": "bitcrushcontrols", "description": "Bit depth / sample rate reducer (lo-fi).",
        "params": {"ingain": P((None, None, None, "Input gain dB")), "innoise": P((None, None, None, "Input noise")),
                   "outgain": P((None, None, None, "Output gain dB")), "outclip": P((None, None, None, "Output clip dB")),
                   "rate": P((None, None, None, "Sample rate in Hz")), "stereodiff": P((None, None, None, "Stereo difference")),
                   "levels": P((None, None, None, "Quantization levels")),
                   "rateon": P((None, None, None, "Rate reduction on")), "depthon": P((None, None, None, "Depth reduction on"))},
    },
    "crossovereq": {
        "node": "crossoevereqcontrols", "description": "4-band crossover equalizer.",
        "params": {"xover12": P((None, None, None, "Band 1/2 crossover Hz")),
                   "xover23": P((None, None, None, "Band 2/3 crossover Hz")),
                   "xover34": P((None, None, None, "Band 3/4 crossover Hz")),
                   "gain1..4": P((None, None, None, "Band gain dB")), "mute1..4": P((None, None, None, "Band enabled (1 = on)"))},
    },
    "delay": {
        "node": "Delay", "description": "Delay / echo with LFO.",
        "params": {"DelayTimeSamples": P((0.5, 0.01, 5, "Delay time in seconds (60/bpm = one beat)")),
                   "FeebackAmount": P((0, 0, 1, "Feedback (sic: 'FeebackAmount')")),
                   "LfoFrequency": P((2, 0.01, 5, "LFO time in seconds")),
                   "LfoAmount": P((0, 0, 0.5, "LFO amount")),
                   "OutGain": P((0, -60, 20, "Output gain dB"))},
    },
    "dualfilter": {
        "node": "DualFilterControls", "description": "Two filters in parallel with mix.",
        "params": {"enabled1": P((1, 0, 1, "Filter 1 on")), "filter1": P((0, 0, 21, "Filter 1 type index (see filter types)")),
                   "cut1": P((7000, 1, 20000, "Cutoff 1 Hz")), "res1": P((0.5, 0.01, 10, "Resonance 1")),
                   "gain1": P((100, 0, 200, "Gain 1 %")), "mix": P((0, -1, 1, "Mix: -1 filter 1 only, 1 filter 2 only")),
                   "enabled2": P((1, 0, 1, "Filter 2 on")), "filter2": P((0, 0, 21, "Filter 2 type index")),
                   "cut2": P((7000, 1, 20000, "Cutoff 2 Hz")), "res2": P((0.5, 0.01, 10, "Resonance 2")),
                   "gain2": P((100, 0, 200, "Gain 2 %"))},
    },
    "dynamicsprocessor": {
        "node": "dynamicsprocessor_controls", "description": "Curve based dynamics processor.",
        "params": {"inputGain": P((1, 0, 5, "Input gain")), "outputGain": P((1, 0, 5, "Output gain")),
                   "attack": P((10, 1, 500, "Attack ms")), "release": P((100, 1, 500, "Release ms")),
                   "stereoMode": P((0, 0, 2, "0 max, 1 average, 2 unlinked"))},
    },
    "eq": {
        "node": "Eq", "description": "8-band parametric equalizer (HP, low shelf, 4 peaks, high shelf, LP). "
                                     "A band only works when its '...active' flag is 1.",
        "params": {
            "HPactive, HPfreq, HPres": P((None, None, None, "High-pass: active 0/1, freq 20-20000 Hz (31), res (0.707)")),
            "Lowshelfactive, LowShelffreq, Lowshelfgain, LowShelfres": P((None, None, None, "Low shelf: freq (80), gain -18..18 dB")),
            "Peak1active, Peak1freq, Peak1gain, Peak1bw": P((None, None, None, "Peak 1 (120 Hz), gain -18..18 dB, bandwidth 0.1-4 octaves")),
            "Peak2active, Peak2freq, Peak2gain, Peak2bw": P((None, None, None, "Peak 2 (250 Hz)")),
            "Peak3active, Peak3freq, Peak3gain, Peak3bw": P((None, None, None, "Peak 3 (2000 Hz)")),
            "Peak4active, Peak4freq, Peak4gain, Peak4bw": P((None, None, None, "Peak 4 (4000 Hz)")),
            "Highshelfactive, Highshelffreq, HighShelfgain, HighShelfres": P((None, None, None, "High shelf (12000 Hz)")),
            "LPactive, LPfreq, LPres": P((None, None, None, "Low-pass (18000 Hz)")),
            "Inputgain, Outputgain": P((0, -60, 20, "Gains in dB")),
        },
    },
    "flanger": {
        "node": "Flanger", "description": "Flanger.",
        "params": {"DelayTimeSamples": P((0.001, 0.0001, 0.05, "Delay in seconds")),
                   "LfoFrequency": P((0.25, 0.01, 60, "LFO period in seconds")),
                   "LfoAmount": P((0, 0, 0.0025, "LFO amount in seconds")),
                   "LfoPhase": P((90, 0, 360, "Stereo phase")),
                   "Feedback": P((0, -1, 1, "Feedback")), "WhiteNoise": P((0, 0, 0.05, "Noise")),
                   "Invert": P((0, 0, 1, "Invert feedback"))},
    },
    "multitapecho": {
        "node": "multitapechocontrols", "description": "Multi-tap echo.",
        "params": {"steps": P((None, None, None, "Number of taps")), "steplength": P((None, None, None, "Tap length ms")),
                   "drygain": P((None, None, None, "Dry gain dB")), "swapinputs": P((None, None, None, "Swap inputs")),
                   "stages": P((None, None, None, "Low-pass stages"))},
    },
    "reverbsc": {
        "node": "ReverbSCControls", "description": "Smooth algorithmic reverb. Typical use: wet 0.2-0.4.",
        "params": {"input_gain": P((0, -60, 15, "Input gain dB")), "size": P((0.89, 0, 1, "Room size / decay")),
                   "color": P((10000, 100, 15000, "Brightness (low-pass Hz)")),
                   "output_gain": P((0, -60, 15, "Output gain dB"))},
    },
    "stereoenhancer": {
        "node": "stereoenhancercontrols", "description": "Stereo widener.",
        "params": {"width": P((0, 0, 180, "Width"))},
    },
    "stereomatrix": {
        "node": "stereomatrixcontrols", "description": "Stereo routing matrix (mono-ize, swap, narrow).",
        "params": {"l-l": P((1, -1, 1, "Left to left")), "l-r": P((0, -1, 1, "Left to right")),
                   "r-l": P((0, -1, 1, "Right to left")), "r-r": P((1, -1, 1, "Right to right"))},
    },
    "waveshaper": {
        "node": "waveshapercontrols", "description": "Waveshaping distortion (default curve is linear; "
                                                     "raise inputGain with clipInput=1 for hard clipping).",
        "params": {"inputGain": P((1, 0, 5, "Input gain")), "outputGain": P((1, 0, 5, "Output gain")),
                   "clipInput": P((0, 0, 1, "Clip input"))},
    },
    "compressor": {
        "node": "CompressorControls", "min_version": "1.3",
        "description": "Full-featured compressor / limiter (LMMS 1.3+ only).",
        "params": {"threshold": P((-8, -60, 0, "Threshold dB")), "ratio": P((1.8, 1, 20, "Ratio")),
                   "attack": P((10, 0.005, 250, "Attack ms")), "release": P((100, 1, 2500, "Release ms")),
                   "knee": P((12, 0, 96, "Knee dB")), "outGain": P((0, -60, 30, "Output (makeup) gain dB")),
                   "inGain": P((0, -60, 30, "Input gain dB")), "limiter": P((0, 0, 1, "Limiter mode")),
                   "autoMakeup": P((0, 0, 1, "Auto makeup gain")), "mix": P((100, 0, 100, "Dry/wet %"))},
    },
    "dispersion": {
        "node": "DispersionControls", "min_version": "1.3", "description": "Dispersion / all-pass phaser (1.3+).",
        "params": {"amount": P((0, 0, 999, "Number of filters")), "freq": P((200, 20, 20000, "Frequency")),
                   "reso": P((0.707, 0.01, 8, "Resonance")), "feedback": P((0, -1, 1, "Feedback"))},
    },
    "lomm": {"node": "LOMMControls", "min_version": "1.3",
             "description": "Upwards/downwards multiband compressor (OTT style, 1.3+).",
             "params": {"depth": P((0.4, 0, 1, "Depth")), "time": P((1, 0, 10, "Time")),
                        "inVol": P((0, -48, 48, "Input dB")), "outVol": P((8, -48, 48, "Output dB"))}},
    "slewdistortion": {"node": "SlewDistortionControls", "min_version": "1.3",
                       "description": "Multiband slew-rate distortion (1.3+).", "params": {}},
    "frequencyshifter": {"node": "FrequencyShifterControls", "min_version": "1.3",
                         "description": "Frequency shifter (recent 1.3 builds).", "params": {}},
    "granularpitchshifter": {"node": "GranularPitchShifterControls", "min_version": "1.3",
                             "description": "Granular pitch shifter (1.3+).",
                             "params": {"pitch": P((1, -48, 24, "Pitch in semitones"))}},
}

# Drum kits built from the factory samples that ship with every LMMS version.
DRUM_KITS: dict[str, dict[str, str]] = {
    "standard": {"kick": "drums/kick01.ogg", "snare": "drums/snare01.ogg",
                 "hihat": "drums/hihat_closed01.ogg", "openhat": "drums/hihat_opened01.ogg",
                 "clap": "drums/clap01.ogg", "crash": "drums/crash01.ogg",
                 "ride": "drums/ride01.ogg", "tom": "drums/tom01.ogg", "rim": "drums/rim01.ogg",
                 "shaker": "drums/shaker01.ogg"},
    "acoustic": {"kick": "drums/bassdrum_acoustic01.ogg", "snare": "drums/snare_acoustic01.ogg",
                 "hihat": "drums/hihat_closed02.ogg", "openhat": "drums/hihat_opened02.ogg",
                 "crash": "drums/crash02.ogg", "ride": "drums/ride02.ogg",
                 "tom_hi": "drums/tom_hi01.ogg", "tom_mid": "drums/tom_mid01.ogg",
                 "tom_low": "drums/tom_low01.ogg", "rim": "drums/sidestick01.ogg"},
    "hiphop": {"kick": "drums/kick_hiphop01.ogg", "snare": "drums/snare_hiphop01.ogg",
               "hihat": "drums/hihat_closed03.ogg", "openhat": "drums/hihat_opened03.ogg",
               "clap": "drums/clap02.ogg", "rim": "drums/snare_rim01.ogg",
               "shaker": "drums/shaker02.ogg"},
    "electro": {"kick": "drums/kick_hard01.ogg", "snare": "drums/snare_electro01.ogg",
                "hihat": "drums/hihat_closed04.ogg", "openhat": "drums/hihat_opened01.ogg",
                "clap": "drums/clap03.ogg", "zap": "drums/zap01.ogg", "crash": "drums/crash01.ogg"},
    "hard": {"kick": "drums/kick_hardcore01.ogg", "snare": "drums/snare_harsh01.ogg",
             "hihat": "drums/hihat_closed05.ogg", "openhat": "drums/hihat_opened02.ogg",
             "clap": "drums/clap04.ogg", "crash": "drums/crash02.ogg"},
}


def wave_table(shape: str, length: int = 128) -> str:
    """Base64 float32 single-cycle wave for BitInvader."""
    shape = shape.strip().lower()
    values = []
    seed = 12345
    for i in range(length):
        x = i / length
        if shape in ("sine", "sin"):
            v = math.sin(2 * math.pi * x)
        elif shape in ("saw", "sawtooth"):
            v = 2 * x - 1
        elif shape in ("square", "pulse"):
            v = 1.0 if x < 0.5 else -1.0
        elif shape in ("triangle", "tri"):
            v = 4 * x - 1 if x < 0.5 else 3 - 4 * x
        elif shape == "noise":
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            v = seed / 0x3FFFFFFF - 1
        else:
            raise ValueError("Unknown wave shape. Use sine, saw, square, triangle or noise.")
        values.append(v)
    return base64.b64encode(struct.pack(f"<{length}f", *values)).decode("ascii")


def describe(spec_params: dict) -> dict:
    out = {}
    for name, (default, lo, hi, text) in spec_params.items():
        entry = {"description": text}
        if default is not None:
            entry["default"] = default
        if lo is not None:
            entry["range"] = [lo, hi]
        out[name] = entry
    return out
