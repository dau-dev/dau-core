// Element types for the tile palette.
//
// A tile that filters, compares or accumulates does not care that a value is
// "32 bits". It cares that the value is a SIGNED INTEGER 32 bits wide, or a
// Q16.16 fixed-point number, or an IEEE-754 binary32 -- three different
// meanings the single integer 32 cannot tell apart. This header gives those
// meanings names, so a tile is written once against a TYPE and derives the
// width from it, instead of being written against a width and assuming the
// rest.
//
// THE ENCODING. A type is one plain `int` parameter, because that is the only
// thing a SystemVerilog parameter override, a Vivado `-generic` and a
// `-G` on the simulator command line can all carry. Its bits are:
//
//     [7:0]   total width in bits (1..255)
//     [15:8]  fractional bits (0 for integers and for floats)
//     [16]    1 = IEEE-754 binary floating point
//     [17]    1 = unsigned
//
// A PLAIN SIGNED INTEGER THEREFORE ENCODES AS ITS OWN WIDTH: `DAU_TYPE_INT32
// is 32, exactly the number tiles carried before this header existed. That is
// deliberate and load-bearing -- every measured resource envelope, every
// staged design and every `-generic FIELD_WIDTH=32` keeps its meaning, and
// migrating a tile from a width to a type changes no synthesized bit. Only
// the types a width could never express (fixed point, float, unsigned) encode
// to something larger.
//
// Q NOTATION IS AMBIGUOUS IN THE WILD, so pin it here: qI.F has I integer
// bits INCLUDING the sign bit and F fractional bits, and occupies I+F bits in
// total. q16.16 is the 32-bit format the fixed-point divide and sqrt tiles
// compute in (what they spell as FRACTIONAL_BITS=16).
//
// A TYPE IS NOT A CAPABILITY. Naming float64 here does not give any tile
// floating-point arithmetic; it gives tiles a way to SAY they were handed one.
// A tile whose datapath is an integer comparator should guard on
// `DAU_TYPE_IS_FLOAT and refuse, rather than silently comparing exponent bits
// as though they were magnitude.
//
// dau_core.element_types is this encoding's Python half (the registry parses
// "int32"/"q16.16" spellings into these same codes), and a test holds the two
// to the same numbers.

`ifndef DAU_TYPES_SVH
`define DAU_TYPES_SVH

// Constructors: build a type from its parts.
`define DAU_TYPE_INT(w) (w)
`define DAU_TYPE_UINT(w) ((w) | (1 << 17))
`define DAU_TYPE_Q(i, f) (((i) + (f)) | ((f) << 8))
`define DAU_TYPE_FLOAT(w) ((w) | (1 << 16))

// The palette's named types.
`define DAU_TYPE_INT8 `DAU_TYPE_INT(8)
`define DAU_TYPE_INT16 `DAU_TYPE_INT(16)
`define DAU_TYPE_INT32 `DAU_TYPE_INT(32)
`define DAU_TYPE_INT64 `DAU_TYPE_INT(64)
`define DAU_TYPE_FLOAT32 `DAU_TYPE_FLOAT(32)
`define DAU_TYPE_FLOAT64 `DAU_TYPE_FLOAT(64)
`define DAU_TYPE_Q16_16 `DAU_TYPE_Q(16, 16)

// Accessors: the four facts a tile needs from a type. Each yields a plain
// integer so it can size a vector, index a slice, or drive an elaboration
// guard directly.
`define DAU_TYPE_WIDTH(t) ((t) & 32'hFF)
`define DAU_TYPE_FRAC_BITS(t) (((t) >> 8) & 32'hFF)
`define DAU_TYPE_IS_FLOAT(t) (((t) >> 16) & 32'h1)
// stated as "not unsigned" rather than a signed bit of its own, so that a
// plain signed integer's code stays equal to its width (see THE ENCODING)
`define DAU_TYPE_IS_SIGNED(t) (1 - (((t) >> 17) & 32'h1))

`endif
