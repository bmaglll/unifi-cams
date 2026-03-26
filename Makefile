PREFIX ?= $(HOME)/.local/bin

install:
	mkdir -p $(PREFIX)
	ln -sf $(CURDIR)/unifi-stream.sh $(PREFIX)/unifi-stream
	ln -sf $(CURDIR)/unifi-dashboard.sh $(PREFIX)/unifi-dashboard

uninstall:
	rm -f $(PREFIX)/unifi-stream $(PREFIX)/unifi-dashboard
