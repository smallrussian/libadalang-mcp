with Ada.Text_IO;

package body Vehicle is

   procedure Log (Msg : String) is
   begin
      Ada.Text_IO.Put_Line (Msg);
   end Log;

   procedure Log (Value : Integer) is
   begin
      Log (Integer'Image (Value));
   end Log;

   function Distance (A, B : Position) return Natural is
   begin
      return abs (A.X - B.X) + abs (A.Y - B.Y);
   end Distance;

end Vehicle;
