package body Utils is

   function Clamp (V : Vehicle.Speed) return Vehicle.Speed is
   begin
      return Vehicle.Speed'Min (V, Vehicle.Speed'Last);
   end Clamp;

end Utils;
