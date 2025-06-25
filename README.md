# aurdex

**aurdex** is a terminal user interface (TUI) for browsing AUR package metadata.

- Filter and search packages by name, maintainer, status, and more
- View detailed metadata, dependencies, and reverse dependencies
- Explore package git repositories and commit history

> **Note**: aurdex is **not** an AUR helper. It does **not** install or build packages.  
> It’s designed for **viewing package information** and understanding dependency relationships.
![AUR_Package_Browser_2025-06-24T20_40_26_492693](https://github.com/user-attachments/assets/d63b2ba5-e6cb-4c4d-a31e-6b355120fdcb)
![AUR_Package_Browser_2025-06-24T20_40_37_345778](https://github.com/user-attachments/assets/cf1bcaba-79b1-47f6-99d8-7afeb6105611)
![AUR_Package_Browser_2025-06-24T20_40_43_800563](https://github.com/user-attachments/assets/11398438-f363-49f2-a42b-7ae9ce433228)
![AUR_Package_Browser_2025-06-24T20_42_26_423139](https://github.com/user-attachments/assets/bece2564-7133-4436-a665-144311c83e29)

# Installation
The aurdex tool is available in the AUR (Arch User Repository).

You can install it using an AUR helper such as yay, paru, or manually via git.

### Using an AUR helper (recommended)
```
yay -S aurdex

or

paru -S aurdex
```
### Manual installation
Clone the package and build it with makepkg:
```
git clone https://aur.archlinux.org/aurdex.git
cd aurdex
makepkg -si
```
