with Vehicle;        use Vehicle;
with Vehicle.Engine;
with Garage;

--  Body-only main procedure (no spec file).
procedure Main_Proc is
   Car  : aliased Vehicle.Engine.Engine;
   Slot : Garage.Slot;
begin
   Vehicle.Engine.Start (Car);
   Vehicle.Engine.Accelerate (Car, 10);
   Garage.Park (Slot, Car'Access);
   Log ("speed:");
   Log (Integer (Car.Current));
   if Vehicle.Engine.Is_Running (Car) then
      Log (Distance (Car.Where, (X => 3, Y => 4, Label => "target  ")));
   end if;
end Main_Proc;
