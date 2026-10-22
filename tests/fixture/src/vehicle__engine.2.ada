with Utils;

package body Vehicle.Engine is

   procedure Start (E : in out Engine) is separate;

   procedure Accelerate (E : in out Engine; By : Speed) is
   begin
      E.Current := Utils.Clamp (E.Current + By);
      Log (Integer (E.Current));
   end Accelerate;

   function Is_Running (E : Engine) return Boolean is (E.Running);

end Vehicle.Engine;
