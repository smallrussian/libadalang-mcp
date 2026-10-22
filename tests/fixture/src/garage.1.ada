limited with Vehicle.Engine;

--  Uses a limited view of Vehicle.Engine.
package Garage is

   type Slot is record
      Occupant : access Vehicle.Engine.Engine;
      Number   : Positive := 1;
   end record;

   procedure Park (S : in out Slot; E : access Vehicle.Engine.Engine);

end Garage;
