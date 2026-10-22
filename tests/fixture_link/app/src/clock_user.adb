with Ada.Calendar;

--  Body-only unit depending on the GNAT runtime (Ada.Calendar imports C routines).
procedure Clock_User is
   T : constant Ada.Calendar.Time := Ada.Calendar.Clock;
   pragma Unreferenced (T);
begin
   null;
end Clock_User;
