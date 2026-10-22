with Interfaces;

--  Unit under test: bit operations on a 16-bit word.
package Bit_Ops is

   type Word is mod 2**16;
   subtype Bit_Index is Natural range 0 .. 15;

   Bit_Error : exception;

   Calls : Natural := 0;
   --  Global state: counts Set_Bit calls.

   function Set_Bit (W : Word; Bit : Bit_Index) return Word
     with Pre => Bit <= 15;

   function Shift (Value : Interfaces.Unsigned_32; Count : Natural)
     return Interfaces.Unsigned_32;

   function Rotate (W : Word; Count : Natural) return Word;

   type Sensor is abstract tagged null record;
   function Read (S : Sensor) return Word is abstract;

   function Checked_Read (S : Sensor'Class; Limit : Word) return Word;

end Bit_Ops;
