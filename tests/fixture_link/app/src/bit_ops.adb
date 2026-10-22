with Hal;

package body Bit_Ops is

   function Set_Bit (W : Word; Bit : Bit_Index) return Word is
   begin
      Calls := Calls + 1;
      if Bit > 15 then
         raise Bit_Error with "bit index out of range";
      elsif W = Word'Last then
         return W;
      end if;
      return W or Word (2**Bit);
   end Set_Bit;

   function Shift (Value : Interfaces.Unsigned_32; Count : Natural)
     return Interfaces.Unsigned_32 is
   begin
      if Count = 0 then
         return Value;
      end if;
      Hal.Hal_Reset;
      return Hal.Bit_Shift_Left (Value, Count);
   end Shift;

   function Rotate (W : Word; Count : Natural) return Word is
      use type Interfaces.Unsigned_16;
      R : constant Interfaces.Unsigned_16 :=
        Interfaces.Rotate_Left (Interfaces.Unsigned_16 (W), Count mod 16);
   begin
      return Word (R);
   end Rotate;

   function Checked_Read (S : Sensor'Class; Limit : Word) return Word is
      V : constant Word := S.Read;
   begin
      case V is
         when 0 =>
            raise Constraint_Error with "sensor returned zero";
         when others =>
            if V > Limit then
               return Limit;
            end if;
            return V;
      end case;
   exception
      when Bit_Error =>
         return 0;
   end Checked_Read;

end Bit_Ops;
