with Interfaces;

--  Bindings to C routines in hal_bits.c.
package Hal is

   --  Aspect form, with an explicit external name (Fortran-style symbol).
   function Bit_Shift_Left (Value : Interfaces.Unsigned_32; Count : Natural)
     return Interfaces.Unsigned_32
     with Import, Convention => C, External_Name => "ibslft_";

   --  Pragma form, default external name.
   procedure Hal_Reset;
   pragma Import (C, Hal_Reset, "hal_reset");

   --  Imported, but no C definition exists anywhere: link would fail.
   function Hal_Missing return Integer;
   pragma Import (C, Hal_Missing, "hal_missing_symbol");

end Hal;
