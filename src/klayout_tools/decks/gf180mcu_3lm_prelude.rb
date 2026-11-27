# mslot.rb reads Metal4 even on 3LM, where generic_layers.rb omits it.
# Keep the fallback on the receiver the foundry deck uses for layer lookups.
module RuleFramework
  class DeckEnv
    def metal4_drawn
      return ctx[:metal4_drawn] if ctx.key?(:metal4_drawn)

      polygon_layer
    end
  end
end
