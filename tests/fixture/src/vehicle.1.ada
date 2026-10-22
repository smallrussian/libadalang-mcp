--  Parent package.
package Vehicle is

   Max_Speed : constant := 300;

   type Speed is range 0 .. Max_Speed;

   type Position is record
      X, Y  : Integer := 0;
      Label : String (1 .. 8) := (others => ' ');
   end record;

   procedure Log (Msg : String);
   procedure Log (Value : Integer);
   --  Two overloads: name resolution must pick the right one.

   function Distance (A, B : Position) return Natural;

end Vehicle;
