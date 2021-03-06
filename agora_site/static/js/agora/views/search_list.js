(function() {
    var Agora = this.Agora,
        app = this.app;

    /*
     * View for search pages.
    */
    (function() {
        var SearchInfiniteView = Agora.GenericListView.extend({
            el: "#activity-list",
            templateEl: "#template-search-item",

            setup: function() {
                Agora.GenericListView.prototype.setup.apply(this);
                this.params = {q: this.$el.data('query')};

                this.templates = {}
                ctypes = ["agora", "election", "profile"];
                for (var i = 0; i < ctypes.length; i++) {
                    var templateEl = "#template-search-"+ ctypes[i] +"-item";
                    this.templates[ctypes[i]] = _.template($(templateEl).html());
                }
            },

            renderItem: function(model) {
                var templateEl = this.templateEl;
                var ctype = model.get('obj').content_type;
                var template = this.templates[ctype];
                var ret = template(model.toJSON().obj);
                return ret;
            }
        });

        Agora.SearchListView = Backbone.View.extend({
            el: "div.search",

            initialize: function() {
                _.bindAll(this);
                this.infiniteListView = new SearchInfiniteView();
            }
        });
    }).call(this);
}).call(this);
